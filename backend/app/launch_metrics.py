"""上线效果统计（绩效管理 → 上线效果）：智能客服上线后的来信与回复、AI 草稿、客服效率、售后与物流、大模型成本。

全部只读、全部读库（不调 eBay、不调大模型）。时间范围按本机日期闭区间 [start, end]；stores 为 None 表示全部店铺，
否则只统计这些店铺（客服只看自己负责的店铺）。每个指标的口径写在 METRIC_NOTES 里，页面原样显示。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import round_metrics
from .guards.risk import GATE_INFO_REASONS, REASON_LABELS
from .intents import INTENTS
from .sending.service import ACK_PREFIX, is_ack
from .models import (AftersalesCase, Conversation, Draft, Invoice, InvoiceStatus, JobState, LlmUsage, LogisticsTicket, Message,
                     OutboundMessage, ParcelFollowup, ReplyJob, ReviewFeedback)

SYSTEM_TYPE = "FROM_EBAY"  # eBay 系统通知会话，不算买家来信
# “已收到”回执不算回复：标了 auto_kind=ACK 的，以及只有 SIM-OUT-ACK- 前缀、没标 auto_kind 的旧回执（和工作台 is_ack 一致）
NOT_ACK = ((Message.auto_kind.is_(None)) | (Message.auto_kind != "ACK")) & ~Message.ebay_message_id.startswith(ACK_PREFIX)
LATE_SECONDS = 24 * 3600
# AI 草稿修改幅度分档（edit_ratio = 1 - 相似度；0 = 一字不改）。“采纳” = 原样 + 微调，和工作台指标的 10% 口径一致
EDIT_BUCKETS = [("adopted", "原样采用"), ("tweaked", "微调（<10%）"), ("edited", "小改（10%–40%）"), ("rewritten", "大改（≥40%）")]


def _bucket(ratio: float) -> str:
    if ratio <= 0:
        return "adopted"
    if ratio < 0.1:
        return "tweaked"
    return "edited" if ratio < 0.4 else "rewritten"
LLM_PURPOSES = {"reply": "回复生成", "repair": "回复格式修正", "translate": "翻译", "vision": "识图",
                "embedding_query": "检索向量", "embedding_docs": "知识向量化", "ticket_reply": "查件结果起草",
                "data_assistant_plan": "数据助手", "check": "连通测试"}
NO_DRAFT_LABELS = {"RUN_FAILED": "运行失败（重试多次后转人工）", "IN_PROGRESS": "还在生成或等待重试", "UNKNOWN": "未记录原因"}

METRIC_NOTES = {
    "buyer_messages": "买家发来的消息条数（不含 eBay 系统通知），按消息时间统计",
    "conversations": "这段时间有买家来信的会话数",
    "replied_conversations": "这段时间有买家来信、之后店铺回复过的会话数（“已收到”回执不算回复）",
    "first_response": "首次回复时长：买家来信（连续多条从第一条算）→ 店铺下一条正式回复；中位数 / P90 只算已回复的",
    "within_24h": "24 小时内回复的比例：首次回复时长 ≤ 24 小时 ÷ 全部来信轮次（没回复的也算在分母里）",
    "unreplied_now": "当前最后一句是买家、店铺还没正式回复的会话（实时，不受时间范围影响）",
    "sent_via_system": "经本系统发件箱真正发到买家的消息，按来源分（人工 / AI 自动 / 已收到回执 / 查件结果 / 发货通知等）",
    "jobs": "买家来信生成的回复任务数（被新来信合并作废的不算）",
    "drafted": "AI 起草了草稿的任务数；没起草的按原因分布见下表",
    "review": "客服审核 AI 草稿后发出的方式：修改比例 = 1 − 与 AI 草稿的文字相似度；人工撰写 = 没用 AI 草稿",
    "adoption_rate": "采纳率 = （原样采用 + 微调）÷（原样 + 修改后发 + 人工撰写）",
    "avg_edit_ratio": "平均修改比例：用了 AI 草稿发出的回复，修改比例的平均值（0 = 一字不改）",
    "no_draft": "AI 没起草的任务按原因分（黑名单、模型未配置、模型调用失败、非对接店铺等）",
    "intents": "AI 识别的买家问题类型（每个任务取最后一版草稿的主要意图）",
    "handoff_reasons": "需要人工审核的原因（每个任务去重计数；“自动发送未开启”等闸门说明不计）",
    "staff": "每位客服审核 / 发出的回复数、处理时长（打开会话到提交，中位数），撤回和发送失败来自发件箱",
    "tickets": "谷仓查件单：按谷仓创建时间统计；平均回复时长 = 谷仓回复时间 − 创建时间",
    "feishu": "飞书售后工单：按发到群里的时间统计；群内答复时长 = 群里第一条回复 − 发群时间；回复买家时长 = 回复买家 − 发群时间",
    "parcels": "异常包裹跟进：新增按创建时间、完结按完结时间统计；平均处理天数 = 完结 − 创建（只算这段时间完结的）",
    "invoices": "生成的发票：按生成时间统计，AI 自动 = 系统自动开具并发出",
    "llm": "大模型调用：按调用时间统计，token 为服务商返回值（向量模型不返回时按字数估算）",
    "bands": "首次响应按买家来信到达时段分：工作时段 = 北京时间周一至周五 9:00–18:00，夜间 = 工作日其余时间，周末 = 周六日；"
             "中位数 / P90 只算已回复的轮次",
    "on_time": f"按时率：首次响应 ≤ {round_metrics.RESPONSE_SLA_HOURS} 小时的轮次 ÷（按时 + 超时，含超过时限还没回的）；"
               "还没回、也还没到时限的算观察中",
    "follow_up": f"买家追问率：店铺回复后 {round_metrics.FOLLOW_UP_HOURS} 小时内买家在同一会话又来信的轮次 ÷ 已回复轮次；"
                 "只是致谢 / 结束语的来信不算追问（有 AI 意图用 thanks_closing，没有时按多语言致谢说法判断，"
                 "带退款 / 退货 / 发错 / 坏了 / 不适配等投诉诉求词的不算致谢）；店铺这次回复是在请买家补资料"
                 "（VIN / 车架号、照片 / 视频、订单号、车型年款、邮箱等）的，买家补发过来不算追问；"
                 "“接受报价”“换了新问题”目前分不出来，仍算追问；"
                 "回复后还不满 48 小时的算观察中，不计入分母",
    "escalation": f"沟通后升级率：店铺回复后 {round_metrics.ESCALATION_DAYS} 天内，同店铺同买家新开 eBay 售后的轮次 ÷ 已回复轮次。"
                  "算：未收到货咨询、纠纷全部；退货只算商品或发货问题（有缺陷、到货损坏、与描述不符、缺件、发错货、不适配），"
                  "买家自己的原因（不想要了、买错、尺寸不对、找到更便宜的等）不算；付款纠纷算没收到、与描述严重不符、没收到退款，"
                  "欺诈和交易问题不算。一张售后只算一轮：归到它开启前最近的那一轮店铺回复（7 天内）。回复后还不满 7 天的算观察中",
    "north_star": f"北极星“按时且有效的回复占比”：按时（{round_metrics.RESPONSE_SLA_HOURS} 小时内首次回复）且 48 小时内没被追问、"
                  "7 天内没升级的轮次 ÷ 需回复的来信轮次；超时没回、被追问或升级直接算不达标，按时但窗口还没走完的算观察中",
    "night_backlog": "非工作时段积压（含周末）：工作日夜间或周末到达、到工作时段才首次回复的轮次（占非工作时段来信的比例、按周折算）；"
                     "另列非工作时段到达、还没回复的",
    "volume": "买家消息量：按店铺、北京时间每小时、星期几统计（不含 eBay 系统通知）",
    "baseline": "基线 = 上线前纯人工时期（1 号从 eBay 补拉的历史消息，baseline_messages），和上线后用同一套口径计算："
                "每轮第一条买家消息配之后第一条店铺消息，连续多条店铺回复只算第一条，追问 / 升级窗口也从这条算起；"
                "基线结果按批次 + 店铺存快照（不含正文），原始消息清理后仍可对比；单个店铺的快照只看包含这个店铺的批次，"
                "之后补拉别的店铺不影响它；全部店铺合计在原始消息不完整时沿用之前的快照。上线后取页面选的时间段。"
                "样本量是这一项实际用到的轮次数，任一边少于 30 标“样本少，仅供参考”。"
                "升级率要用到 eBay 售后单：某店铺算升级的售后（咨询 / 纠纷 / 退货 / 付款纠纷）最早开启时间晚于基线起点、"
                "又没有基线补拉的单时，升级率和北极星标“售后数据不全，仅供参考”；售后补拉后快照自动重算。人均处理量、草稿采纳率、审核时长纯人工时期没有记录",
}


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _local_day(dt: datetime) -> date:
    return _utc(dt).astimezone().date()


def _pct(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _p90(values: list[float]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return int(s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))])


def _med(values: list[float]) -> int | None:
    return int(median(values)) if values else None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _dist(counter: Counter, labels: dict[str, str] | None = None, total: int | None = None) -> list[dict]:
    base = total if total is not None else sum(counter.values())
    return [{"key": k, "label": (labels or {}).get(k, k), "count": n, "share": _pct(n, base)}
            for k, n in counter.most_common()]


class _Range:
    """本机日期闭区间；查库时 UTC 前后各放宽一天，再按本机日期精确过滤。"""

    def __init__(self, start: date, end: date, stores: list[str] | None):
        self.start, self.end, self.stores = start, end, stores
        self.lo = datetime.combine(start - timedelta(days=1), datetime.min.time())
        self.hi = datetime.combine(end + timedelta(days=2), datetime.min.time())

    def has(self, dt: datetime | None) -> bool:
        return dt is not None and self.start <= _local_day(dt) <= self.end

    def scoped(self, q, col):
        """店铺范围（没有店铺的记录只有看全部店铺时才算）。"""
        if self.stores is None:
            return q
        return q.where(col.in_(self.stores))

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


# ------------------------------------------------------------------ 来信与回复
def _messages(db: Session, r: _Range) -> dict:
    conv_q = r.scoped(select(Conversation.id).where(Conversation.conversation_type != SYSTEM_TYPE), Conversation.store_id)
    buyer = [(cid, at) for cid, at in db.execute(
        select(Message.conversation_id, Message.sent_at).where(
            Message.sender_role == "BUYER", Message.direction == "IN", Message.sent_at >= r.lo, Message.sent_at < r.hi,
            Message.conversation_id.in_(conv_q))) if r.has(at)]
    conv_ids = sorted({c for c, _ in buyer})
    first_at = min((at for _, at in buyer), default=None)

    # 来信轮次：买家一条或连续多条消息算一轮，从第一条算起，到店铺下一条正式回复（不含“已收到”回执）
    waits: list[tuple[datetime, datetime | None]] = []
    replied_convs: set[int] = set()
    if conv_ids:
        rows = defaultdict(list)
        for i in range(0, len(conv_ids), 500):
            for cid, role, direction, kind, at, seq, ebay_id in db.execute(
                    select(Message.conversation_id, Message.sender_role, Message.direction, Message.auto_kind,
                           Message.sent_at, Message.conversation_seq, Message.ebay_message_id)
                    .where(Message.conversation_id.in_(conv_ids[i:i + 500]), Message.sent_at >= first_at - timedelta(days=1),
                           Message.sender_role.in_(("BUYER", "SELLER")))):
                rows[cid].append((at, seq, role, direction, kind == "ACK" or is_ack(ebay_id)))
        for cid, msgs in rows.items():
            msgs.sort(key=lambda m: (m[0], m[1]))
            open_at = None
            for at, _seq, role, direction, ack in msgs:
                if role == "BUYER" and direction == "IN":
                    if open_at is None:
                        open_at = at
                elif role == "SELLER" and direction == "OUT" and not ack and open_at is not None:
                    if r.has(open_at):
                        waits.append((open_at, at))
                        replied_convs.add(cid)
                    open_at = None
            if open_at is not None and r.has(open_at):
                waits.append((open_at, None))
    durations = [max(0.0, (b - a).total_seconds()) for a, b in waits if b is not None]

    days = {d: {"date": d.isoformat(), "buyer": 0, "replies": 0} for d in r.days()}
    for _, at in buyer:
        days[_local_day(at)]["buyer"] += 1
    for (at,) in db.execute(select(Message.sent_at).where(
            Message.sender_role == "SELLER", Message.direction == "OUT", Message.sent_at >= r.lo, Message.sent_at < r.hi,
            NOT_ACK, Message.conversation_id.in_(conv_q))):
        if r.has(at):
            days[_local_day(at)]["replies"] += 1

    # 当前最后一句是买家（实时快照）
    last_seq = (select(Message.conversation_id, func.max(Message.conversation_seq).label("seq"))
                .where(Message.sender_role.in_(("BUYER", "SELLER")),
                       NOT_ACK, Message.conversation_id.in_(conv_q))
                .group_by(Message.conversation_id).subquery())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    unreplied = unreplied_late = 0
    for role, at in db.execute(select(Message.sender_role, Message.sent_at).join(
            last_seq, (Message.conversation_id == last_seq.c.conversation_id) & (Message.conversation_seq == last_seq.c.seq))):
        if role == "BUYER":
            unreplied += 1
            unreplied_late += bool(at and (now - at).total_seconds() > LATE_SECONDS)

    sent = Counter()
    for source, at in db.execute(r.scoped(select(OutboundMessage.source, OutboundMessage.sent_at).where(
            OutboundMessage.state == "SENT", OutboundMessage.sent_at >= r.lo, OutboundMessage.sent_at < r.hi),
            OutboundMessage.store_id)):
        if r.has(at):
            sent[source] += 1
    source_labels = {"manual": "人工回复", "auto": "AI 自动回复", "ack": "已收到回执", "ticket": "查件结果",
                     "logistics_notice": "查件提交提醒", "ship_notice": "发货通知", "invoice_notice": "发票提醒"}
    return {
        "buyer_messages": len(buyer), "conversations": len(conv_ids), "replied_conversations": len(replied_convs),
        "reply_rounds": len(waits), "replied_rounds": len(durations),
        "first_response_median": _med(durations), "first_response_p90": _p90(durations),
        "within_24h_rate": _pct(sum(d <= LATE_SECONDS for d in durations), len(waits)),
        "unreplied_now": unreplied, "unreplied_over_24h": unreplied_late,
        "sent_via_system": _dist(sent, source_labels), "sent_via_system_total": sum(sent.values()),
        "days": list(days.values()),
    }


# ------------------------------------------------------------------ AI 草稿效果
def _ai(db: Session, r: _Range) -> dict:
    jobs = [j for j in db.scalars(r.scoped(select(ReplyJob).where(
        ReplyJob.created_at >= r.lo, ReplyJob.created_at < r.hi, ReplyJob.state != JobState.CANCELLED_STALE), ReplyJob.store_id))
        if r.has(j.created_at)]
    ids = [j.id for j in jobs]
    last_draft: dict[int, Draft] = {}
    for i in range(0, len(ids), 500):
        for d in db.scalars(select(Draft).where(Draft.job_id.in_(ids[i:i + 500]), Draft.author == "agent").order_by(Draft.id)):
            last_draft[d.job_id] = d

    no_draft, handoff, intents = Counter(), Counter(), Counter()
    for j in jobs:
        reasons = [x for x in (j.decision_reasons or []) if x not in GATE_INFO_REASONS]
        for code in dict.fromkeys(reasons):
            handoff[code] += 1
        d = last_draft.get(j.id)
        if d is None:
            if reasons:
                for code in dict.fromkeys(reasons):
                    no_draft[code] += 1
            elif j.state == JobState.FAILED_HANDOFF:
                no_draft["RUN_FAILED"] += 1
            elif j.state in (JobState.RECEIVED, JobState.RUNNING, JobState.RETRYABLE_ERROR):
                no_draft["IN_PROGRESS"] += 1
            else:
                no_draft["UNKNOWN"] += 1
        elif d.intent:
            intents[d.intent] += 1

    fb = [f for f in db.scalars(r.scoped(select(ReviewFeedback).join(ReplyJob, ReplyJob.id == ReviewFeedback.job_id).where(
        ReviewFeedback.created_at >= r.lo, ReviewFeedback.created_at < r.hi), ReplyJob.store_id)) if r.has(f.created_at)]
    buckets = Counter()
    ratios = []
    for f in fb:
        if f.action in ("APPROVE", "EDIT_APPROVE"):
            ratio = 0.0 if f.action == "APPROVE" else float(f.edit_ratio or 0)
            ratios.append(ratio)
            buckets[_bucket(ratio)] += 1
        elif f.action == "MANUAL":
            buckets["manual"] += 1
        elif f.action == "REJECT":
            buckets["rejected"] += 1
        elif f.action == "NO_REPLY":
            buckets["no_reply"] += 1
    sent_base = sum(buckets[k] for k in ("adopted", "tweaked", "edited", "rewritten", "manual"))
    labels = dict(EDIT_BUCKETS) | {"manual": "人工撰写（没用 AI 草稿）", "rejected": "拒绝草稿",
                                                            "no_reply": "判定无需回复"}
    order = ["adopted", "tweaked", "edited", "rewritten", "manual", "rejected", "no_reply"]
    return {
        "jobs": len(jobs), "drafted": len(last_draft), "drafted_rate": _pct(len(last_draft), len(jobs)),
        "review": [{"key": k, "label": labels[k], "count": buckets[k],
                    "share": _pct(buckets[k], sent_base) if k not in ("rejected", "no_reply") else None} for k in order],
        "review_sent_total": sent_base,
        "adoption_rate": _pct(buckets["adopted"] + buckets["tweaked"], sent_base),
        "avg_edit_ratio": _avg(ratios),
        "no_draft": _dist(no_draft, REASON_LABELS | NO_DRAFT_LABELS), "no_draft_total": len(jobs) - len(last_draft),
        "intents": _dist(intents, {k: v[0] for k, v in INTENTS.items()}),
        "handoff_reasons": _dist(handoff, REASON_LABELS, total=len(jobs)),
    }


# ------------------------------------------------------------------ 客服效率
def _staff(db: Session, r: _Range) -> list[dict]:
    people: dict[str, dict] = defaultdict(lambda: {"reviewed": 0, "approved": 0, "edited": 0, "manual": 0, "rejected": 0,
                                                   "no_reply": 0, "handling": [], "submitted": 0, "recalled": 0, "failed": 0})
    for f in db.scalars(r.scoped(select(ReviewFeedback).join(ReplyJob, ReplyJob.id == ReviewFeedback.job_id).where(
            ReviewFeedback.created_at >= r.lo, ReviewFeedback.created_at < r.hi), ReplyJob.store_id)):
        if not r.has(f.created_at):
            continue
        p = people[f.reviewer]
        key = {"APPROVE": "approved", "EDIT_APPROVE": "edited", "MANUAL": "manual", "REJECT": "rejected",
               "NO_REPLY": "no_reply"}.get(f.action)
        if key:
            p[key] += 1
            p["reviewed"] += 1
        if f.handling_ms:
            p["handling"].append(f.handling_ms / 1000)
    for actor, state, at in db.execute(r.scoped(select(OutboundMessage.actor, OutboundMessage.state, OutboundMessage.created_at)
                                                .where(OutboundMessage.source == "manual", OutboundMessage.created_at >= r.lo,
                                                       OutboundMessage.created_at < r.hi), OutboundMessage.store_id)):
        if not r.has(at):
            continue
        p = people[actor]
        p["submitted"] += 1
        p["recalled"] += state == "RECALLED"
        p["failed"] += state in ("FAILED", "UNCERTAIN")
    out = []
    for actor, p in people.items():
        h = p.pop("handling")
        out.append({"actor": actor, **p, "median_handling_seconds": _med(h), "avg_handling_seconds": int(sum(h) / len(h)) if h else None})
    return sorted(out, key=lambda x: (-(x["reviewed"] + x["submitted"]), x["actor"]))


# ------------------------------------------------------------------ 售后与物流
def _aftersales(db: Session, r: _Range) -> dict:
    from .logistics.assess import CN, parse_time

    tickets, reply_secs, origin, sent_results = 0, [], Counter(), 0
    for t in db.scalars(r.scoped(select(LogisticsTicket).where(~LogisticsTicket.io_code.like("DRAFT-%")), LogisticsTicket.store_id)):
        added = parse_time(t.added_at, CN) if t.added_at else (_utc(t.submitted_at) if t.submitted_at else None)
        if added is None or not (r.start <= added.astimezone().date() <= r.end):
            continue
        tickets += 1
        origin["AI 起草后提交" if t.origin == "AI_DRAFT" else "谷仓后台 / 人工提交"] += 1
        replied = parse_time(t.replied_at, CN) if t.replied_at else None
        if replied:
            reply_secs.append(max(0.0, (replied - added).total_seconds()))
        sent_results += t.send_status == "SENT"

    feishu, group_secs, answer_secs, cats = 0, [], [], Counter()
    for c in db.scalars(r.scoped(select(AftersalesCase).where(AftersalesCase.sent_at >= r.lo, AftersalesCase.sent_at < r.hi),
                                 AftersalesCase.store_id)):
        if not r.has(c.sent_at):
            continue
        feishu += 1
        cats[c.category] += 1
        times = []
        for rep in c.replies or []:
            try:
                times.append(datetime.fromisoformat(str(rep.get("at")).replace("Z", "+00:00")))
            except (TypeError, ValueError):
                pass
        if times:
            group_secs.append(max(0.0, (min(times) - _utc(c.sent_at)).total_seconds()))
        if c.answered_at:
            answer_secs.append(max(0.0, (c.answered_at - c.sent_at).total_seconds()))

    pf_q = r.scoped(select(ParcelFollowup), ParcelFollowup.store_id)
    new = done = auto_new = 0
    days_to_close = []
    reasons = Counter()
    for p in db.scalars(pf_q.where(((ParcelFollowup.created_at >= r.lo) & (ParcelFollowup.created_at < r.hi)) |
                                   ((ParcelFollowup.closed_at >= r.lo) & (ParcelFollowup.closed_at < r.hi)))):
        if r.has(p.created_at):
            new += 1
            auto_new += p.origin == "AUTO"
            reasons[p.reason] += 1
        if p.status == "DONE" and r.has(p.closed_at):
            done += 1
            days_to_close.append((p.closed_at - p.created_at).total_seconds() / 86400)
    open_now = db.scalar(r.scoped(select(func.count(ParcelFollowup.id)).where(ParcelFollowup.status == "OPEN"),
                                  ParcelFollowup.store_id)) or 0

    inv = [i for i in db.scalars(r.scoped(select(Invoice).where(Invoice.created_at >= r.lo, Invoice.created_at < r.hi),
                                          Invoice.store_id)) if r.has(i.created_at)]
    inv_status = Counter(i.status for i in inv)
    return {
        "tickets": {"count": tickets, "replied": len(reply_secs), "avg_reply_seconds": int(sum(reply_secs) / len(reply_secs)) if reply_secs else None,
                    "median_reply_seconds": _med(reply_secs), "results_sent": sent_results, "origin": _dist(origin)},
        "feishu": {"count": feishu, "group_replied": len(group_secs),
                   "avg_group_reply_seconds": int(sum(group_secs) / len(group_secs)) if group_secs else None,
                   "avg_answer_seconds": int(sum(answer_secs) / len(answer_secs)) if answer_secs else None,
                   "answered": len(answer_secs), "categories": _dist(cats)},
        "parcels": {"new": new, "auto_new": auto_new, "done": done, "open_now": open_now,
                    "avg_days_to_close": round(sum(days_to_close) / len(days_to_close), 1) if days_to_close else None,
                    "reasons": _dist(reasons)},
        "invoices": {"count": len(inv), "auto": sum(i.created_by == "agent" for i in inv),
                     "sent": inv_status["SENT"], "failed": inv_status["SEND_FAILED"],
                     "status": _dist(inv_status, InvoiceStatus.LABELS)},
    }


# ------------------------------------------------------------------ 大模型成本
def _llm(db: Session, r: _Range) -> dict:
    q = select(LlmUsage).where(LlmUsage.created_at >= r.lo, LlmUsage.created_at < r.hi)
    if r.stores is not None:  # 只看自己店铺时，不带店铺的调用（知识向量化、连通测试等）不算
        q = q.where(LlmUsage.store_id.in_(r.stores))
    by: dict[str, dict] = defaultdict(lambda: {"calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
                                               "total_tokens": 0, "estimated": 0})
    for u in db.scalars(q):
        if not r.has(u.created_at):
            continue
        b = by[u.purpose]
        b["calls"] += 1
        b["failed"] += not u.ok
        b["input_tokens"] += u.input_tokens or 0
        b["output_tokens"] += u.output_tokens or 0
        b["total_tokens"] += u.total_tokens or ((u.input_tokens or 0) + (u.output_tokens or 0))
        b["estimated"] += bool(u.estimated)
    rows = sorted(({"purpose": k, "label": LLM_PURPOSES.get(k, k), **v} for k, v in by.items()), key=lambda x: -x["total_tokens"])
    total = {k: sum(x[k] for x in rows) for k in ("calls", "failed", "input_tokens", "output_tokens", "total_tokens")}
    return {"by_purpose": rows, "total": total}


# ------------------------------------------------------------------ 第 2 章指标与基线对比
def _quality(convs: list, keep, cases: dict, now: datetime, days: int) -> dict:
    """keep(时间) → 是否在统计范围内：轮次按开始时间、消息量按消息时间。"""
    rounds = [x for x in round_metrics.build_rounds(convs) if keep(x.start)]
    buyer = [(c.store_id, m.at) for c in convs for m in c.msgs if m.role == "BUYER" and keep(m.at)]
    return round_metrics.compute(rounds, cases, now, buyer, days)


def _live_quality(db: Session, r: _Range, cases: dict, now: datetime) -> dict:
    convs = round_metrics.live_conversations(db, r.stores, r.lo, r.hi)
    return _quality(convs, r.has, cases, now, (r.end - r.start).days + 1)


def _baseline_quality(db: Session, stores: list[str] | None, lo: datetime, hi: datetime, cases: dict, now: datetime) -> dict:
    convs = round_metrics.baseline_conversations(db, stores, lo, hi)
    return _quality(convs, lambda t: lo <= t < hi, cases, now, max(1, (hi - lo).days))


def _observing(q: dict) -> int:
    return sum(q[k]["observing"] for k in ("follow_up", "escalation", "north_star"))


def _snapshot_plan(db: Session):
    """(时间段, 每个范围用的批次号, 原始消息条数, 全部快照)。单个店铺只看包含它的批次（之后补拉别的店铺，它的批次号不变）；
    * = 全部店铺合计，用全部完成的批次。"""
    from .models import BaselineSnapshot

    status, period, batches = round_metrics.baseline_status(db)
    if period is None:
        return None
    stores_of = round_metrics.batch_stores(db, batches)

    def key_for(scope: str) -> str:
        ids = batches if scope == "*" else [b for b in batches if scope in stores_of.get(b, set())] or batches
        return ",".join(str(b) for b in ids)

    snaps = list(db.scalars(select(BaselineSnapshot)))
    return period, key_for, round_metrics.baseline_message_counts(db, *period), snaps


def refresh_baseline_snapshots(db: Session, force: bool = False) -> list[dict]:
    """按“批次 + 店铺”给全部店铺（*）和每个店铺各存一份基线统计快照（不含正文）。
    原始消息完整（每个店铺的条数不少于它以前任何一份快照用到的条数，即没被清理）时，下面情况重算：
    还没有这个批次组合的快照、口径版本变了、计算时还有观察中的轮次、售后补拉后条数变了；force：完整时强制重算。
    原始消息不完整时不重算：已有的快照保留；新的批次组合（如清理后又补拉了别的店铺）先不建，读取时沿用之前的快照。"""
    from .models import BaselineSnapshot

    if not (round_metrics.baseline_available(db) and round_metrics.snapshot_table_ready(db)):
        return []
    plan = _snapshot_plan(db)
    if plan is None:
        return []
    (lo, hi), key_for, counts, snaps = plan
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = {(x.batch_key, x.store_id): x for x in snaps}
    max_count: dict[str, int] = {}
    for x in snaps:
        max_count[x.store_id] = max(max_count.get(x.store_id, 0), x.message_count or 0)
    stores = sorted((set(counts) | {x.store_id for x in snaps}) - {"*"})

    def complete(scope: str) -> bool:
        return all(counts.get(st, 0) >= max_count.get(st, 0) for st in (stores if scope == "*" else [scope]))

    cases = round_metrics.escalation_cases(db, None)
    coverage = round_metrics.case_coverage(db, None, lo, hi)
    out = []
    for scope in ["*", *stores]:
        key = key_for(scope)
        snap, raw = existing.get((key, scope)), counts.get(scope, 0)
        n_cases = sum(c["in_window"] for st, c in coverage.items() if scope == "*" or st == scope)
        ok = complete(scope)
        stale = snap is None or force or snap.rules_version != round_metrics.RULES_VERSION or snap.observing > 0             or (snap.metrics or {}).get("cases_in_window") != n_cases  # 售后补拉后条数变了：重算升级率
        if not raw or not stale or not ok:
            out.append({"store_id": scope, "batch_key": key, "action": "kept" if snap else "skipped",
                        "reason": None if ok else "原始消息已被清理，保留原快照"})
            continue
        q = _baseline_quality(db, None if scope == "*" else [scope], lo, hi, cases, now)
        if snap is None:
            snap = BaselineSnapshot(batch_key=key, store_id=scope)
            db.add(snap)
        snap.period_start, snap.period_end, snap.rules_version = lo, hi, round_metrics.RULES_VERSION
        snap.message_count, snap.observing, snap.metrics = raw, _observing(q), {**q, "cases_in_window": n_cases}
        snap.computed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        out.append({"store_id": scope, "batch_key": key, "action": "computed", "observing": snap.observing})
    db.flush()
    return out


def _baseline(db: Session, stores: list[str] | None, cases: dict, now: datetime) -> tuple[str, dict | None]:
    """上线前基线：(状态, 数据)。状态 READY 有数据 / RUNNING 还在采集 / NONE 没采集（表不存在、没有完成的批次、
    这些店铺没有基线）。优先读快照（全部店铺或单个店铺，先找当前批次组合的，没有就用这个范围最近的一份）；
    快照表还没建（线上库重启前）或是多个店铺的组合时现算。"""
    from .models import BaselineSnapshot

    if not round_metrics.baseline_available(db):
        return "NONE", None
    status, period, batches = round_metrics.baseline_status(db)
    if period is None:
        return status, None
    lo, hi = period
    scope = "*" if stores is None else (stores[0] if len(stores) == 1 else None)
    q, source, computed_at, key = None, "raw", None, None
    if scope is not None and round_metrics.snapshot_table_ready(db):
        refresh_baseline_snapshots(db)
        plan = _snapshot_plan(db)
        want = plan[1](scope) if plan else None
        snap = db.scalar(select(BaselineSnapshot).where(BaselineSnapshot.batch_key == want, BaselineSnapshot.store_id == scope))             or db.scalar(select(BaselineSnapshot).where(BaselineSnapshot.store_id == scope)
                         .order_by(BaselineSnapshot.computed_at.desc()).limit(1))
        if snap is not None:
            q, source, computed_at, key = snap.metrics, "snapshot", snap.computed_at, snap.batch_key
            lo, hi = snap.period_start, snap.period_end
    if q is None:
        q = _baseline_quality(db, stores, lo, hi, cases, now)
    if not q["rounds"] and not q["volume"]["total"]:
        return "NONE", None
    return "READY", {"period_start": lo.date().isoformat(), "period_end": (hi - timedelta(seconds=1)).date().isoformat(),
                     "source": source, "batch_key": key,
                     "computed_at": computed_at.isoformat(timespec="seconds") + "Z" if computed_at else None, **q}


NO_BASELINE = "无（纯人工时期没有记录）"
SMALL_SAMPLE = 30  # 样本少于 30 轮的指标标“样本少，仅供参考”


def _staff_rates(db: Session, r: _Range) -> dict:
    """人均日处理量（每名审核员每个有审核的日子完成的回复任务数的平均）、审核时长中位数。"""
    per: Counter = Counter()
    durations = []
    for f in db.scalars(r.scoped(select(ReviewFeedback).join(ReplyJob, ReplyJob.id == ReviewFeedback.job_id).where(
            ReviewFeedback.created_at >= r.lo, ReviewFeedback.created_at < r.hi,
            ReviewFeedback.action.in_(("APPROVE", "EDIT_APPROVE", "MANUAL", "REJECT"))), ReplyJob.store_id)):
        if r.has(f.created_at):
            per[(f.reviewer, _local_day(f.created_at))] += 1
            if f.handling_ms:
                durations.append(f.handling_ms / 1000)
    return {"per_person_daily": round(sum(per.values()) / len(per), 2) if per else None, "review_median_seconds": _med(durations),
            "reviews": sum(per.values())}


def _compare(base: dict | None, live: dict, ai: dict, staff: dict, missing: str = "基线未采集",
             cases_incomplete: list[str] | None = None) -> list[dict]:
    def band(q, k, f):
        return next((b[f] for b in q["bands"] if b["key"] == k), None) if q else None

    def row(key, label, unit, b, lv, target="—", met=None, note=None):
        change = None
        if isinstance(b, (int, float)) and isinstance(lv, (int, float)) and b:
            change = round((lv - b) / b, 4)
        return {"key": key, "label": label, "unit": unit, "baseline": b, "live": lv, "change": change,
                "target": target, "met": met, "baseline_note": note if b is None else None}

    def le(lv, limit):
        return None if lv is None or limit is None else lv <= limit

    rows = []
    bw, lw = band(base, "work", "median_seconds"), band(live, "work", "median_seconds")
    rows.append(row("work_median", "首次响应中位数（工作时段）", "seconds", bw, lw, "较基线缩短 50%",
                    le(lw, bw * 0.5 if bw is not None else None), None if base else missing))
    for k, name in (("work", "工作时段"), ("night", "夜间"), ("weekend", "周末")):
        if k != "work":
            rows.append(row(f"{k}_median", f"首次响应中位数（{name}）", "seconds", band(base, k, "median_seconds"),
                            band(live, k, "median_seconds")))
        lp = band(live, k, "p90_seconds")
        target, met = ("P3：夜间 P90 ≤ 15 分钟", le(lp, 900)) if k == "night" else ("—", None)
        rows.append(row(f"{k}_p90", f"首次响应 P90（{name}）", "seconds", band(base, k, "p90_seconds"), lp, target, met))
    g = lambda q, a, b: q[a][b] if q else None  # noqa: E731
    rows.append(row("on_time", f"{round_metrics.RESPONSE_SLA_HOURS} 小时内首次回复率", "rate", g(base, "on_time", "rate"),
                    live["on_time"]["rate"]))
    bf, lf = g(base, "follow_up", "rate"), live["follow_up"]["rate"]
    rows.append(row("follow_up", "买家追问率", "rate", bf, lf, "不高于基线", le(lf, bf)))
    be, le_ = g(base, "escalation", "rate"), live["escalation"]["rate"]
    rows.append(row("escalation", "沟通后升级率", "rate", be, le_, "不高于基线", le(le_, be)))
    bn, ln = g(base, "north_star", "rate"), live["north_star"]["rate"]
    rows.append(row("north_star", "按时且有效的回复占比（北极星）", "rate", bn, ln, "较基线提升",
                    None if bn is None or ln is None else ln > bn))
    rows.append(row("night_backlog", "非工作时段积压占比（占非工作时段来信，含周末）", "rate", g(base, "night_backlog", "rate"),
                    live["night_backlog"]["rate"]))
    rows.append(row("night_backlog_week", "非工作时段积压（每周轮次）", "count", g(base, "night_backlog", "per_week"),
                    live["night_backlog"]["per_week"]))
    rows.append(row("volume_day", "日均买家消息", "count", g(base, "volume", "per_day"), live["volume"]["per_day"]))
    rows.append(row("per_person_daily", "人均日处理量（回复任务）", "count", None, staff["per_person_daily"], "较基线提升 50%",
                    None, NO_BASELINE))
    rows.append(row("adoption", "AI 草稿采纳率（修改 < 10%）", "rate", None, ai["adoption_rate"], "≥ 60%",
                    None if ai["adoption_rate"] is None else ai["adoption_rate"] >= 0.6, NO_BASELINE))
    rows.append(row("review_time", "单条审核时长（中位数）", "seconds", None, staff["review_median_seconds"], "较纯人工撰写缩短 50%",
                    None, NO_BASELINE))
    # 样本量：这一项实际用到的分母（轮次；消息量是消息条数）；任一边少于 SMALL_SAMPLE 标“样本少，仅供参考”
    def sample(q, key):
        if not q:
            return None
        if key.endswith(("_median", "_p90")):  # 分时段首次响应：这个时段已回复的轮次
            return next((b["replied"] for b in q["bands"] if b["key"] == key.rsplit("_", 1)[0]), None)
        return {"on_time": q["on_time"]["on_time"] + q["on_time"]["late"],
                "follow_up": q["follow_up"]["followed"] + q["follow_up"]["not_followed"],
                "escalation": q["escalation"]["escalated"] + q["escalation"]["not_escalated"],
                "north_star": q["north_star"]["good"] + q["north_star"]["bad"],
                "night_backlog": q["night_backlog"]["off_hours_rounds"], "night_backlog_week": q["night_backlog"]["off_hours_rounds"],
                "volume_day": q["volume"]["total"]}.get(key)

    for x in rows:
        x["baseline_sample"] = sample(base, x["key"])
        x["live_sample"] = sample(live, x["key"]) if x["key"] not in ("per_person_daily", "adoption", "review_time")             else staff.get("reviews")
        small = [v for v in (x["baseline_sample"], x["live_sample"]) if v is not None and v < SMALL_SAMPLE]
        x["small_sample"] = bool(small)
        x["sample_note"] = "样本少，仅供参考" if small else None
    for x in rows:  # 升级率（以及用到升级的北极星）：售后数据覆盖不到基线起点时只供参考
        x["data_note"] = "售后数据不全，仅供参考" if cases_incomplete and x["key"] in ("escalation", "north_star") else None
    if base is None:
        for x in rows:
            x["baseline_note"] = x["baseline_note"] if x["baseline_note"] == NO_BASELINE else missing
            if x["target"] in ("较基线缩短 50%", "不高于基线", "较基线提升"):
                x["met"] = None
    return rows


def summary(db: Session, start: date, end: date, stores: list[str] | None) -> dict:
    r = _Range(start, end, stores)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cases = round_metrics.escalation_cases(db, stores)
    ai = _ai(db, r)
    quality = _live_quality(db, r, cases, now)
    status, base = _baseline(db, stores, cases, now)
    missing = {"RUNNING": "基线采集中"}.get(status, "基线未采集")
    incomplete: list[str] = []
    if base is not None:
        lo = datetime.fromisoformat(base["period_start"])
        hi = datetime.fromisoformat(base["period_end"]) + timedelta(days=1)
        base_stores = stores if stores is not None else [k["key"] for k in base["volume"]["by_store"]]
        incomplete = round_metrics.cases_incomplete(round_metrics.case_coverage(db, stores, lo, hi), base_stores, lo)
        base = {**base, "cases_incomplete_stores": incomplete}
    return {"start": start.isoformat(), "end": end.isoformat(), "notes": METRIC_NOTES,
            "messages": _messages(db, r), "ai": ai, "staff": _staff(db, r),
            "aftersales": _aftersales(db, r), "llm": _llm(db, r), "quality": quality,
            "baseline": {"available": base is not None, "status": status, "data": base,
                         "comparison": _compare(base, quality, ai, _staff_rates(db, r), missing, incomplete)}}
