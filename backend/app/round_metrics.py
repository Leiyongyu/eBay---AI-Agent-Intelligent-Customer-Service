"""来信轮次口径（开发方案第 2 章）：上线后（messages）和上线前基线（baseline_messages）用同一套计算。

两种数据源先统一成“会话 + 按时间排好的消息”，再算：
- 来信轮次：买家一条或连续多条消息算一轮，从第一条算起，到店铺下一条正式回复（“已收到”回执不算）；
- 首次响应：轮次开始 → 回复，按到达时段（北京时间工作时段 / 夜间 / 周末）分别给中位数和 P90；
- 买家追问：回复后 FOLLOW_UP_HOURS 小时内买家在同一会话又开始新一轮，且这一轮不只是致谢 / 结束语；
  店铺这次回复是在请买家补资料（VIN / 车架号、照片 / 视频、订单号、车型年款、发票邮箱等，is_info_request：
  同一句里既有请求又有资料词，排除“您提供的 / 已发给您 / 请查收 / 告诉您（Ihnen … mitteilen；uns … mitteilen 仍是请求）”这类说法）时，买家下一轮补发过来不算追问；
  但买家下一轮里一点资料都没有（没有 VIN、邮箱、年款 / 号码、资料词、图片说明，也不是“提供不了”，supplies_info）时仍算追问
  （如“按你们的描述应该适配”“What are you selling?”）；“接受报价”“换了新问题”目前分不出来，仍算追问（2026-11-10 定）；
- 沟通后升级：回复后 ESCALATION_DAYS 天内，同店铺同买家新开 eBay 咨询 / 纠纷 / 退货（只算商品或发货问题的原因）/ 付款纠纷；
  一张售后只算一轮：归到它开启前最近的那一轮店铺回复（且在那轮回复后 7 天内），不重复算进更早的轮次（2026-11-10 定）；
- 北极星“按时且有效”：按时（首次响应 ≤ RESPONSE_SLA_HOURS）且没被追问、没升级 ÷ 需回复的来信轮次；
- 窗口还没走完的轮次标“观察中”，不计入对应分母；
- 非工作时段积压（字段名沿用 night_backlog）：工作日夜间或周末到达、到工作时段才首次回复的轮次；
- 消息量：买家消息按店铺、北京时间每小时、星期几。
全部只读；基线表由 1 号定义（models.py 的 BaselineBatch / BaselineConversation / BaselineMessage，补拉见 baseline.py），
表还不存在、没有完成的批次时页面显示“基线未采集 / 基线采集中”。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from .models import Conversation, Draft, EbayCase, Message, ReplyJob, ReplyJobSource
from .sending.service import is_ack

TZ = ZoneInfo("Asia/Shanghai")  # 工作时段按北京时间
WORK_DAYS = (0, 1, 2, 3, 4)  # 周一到周五
WORK_START, WORK_END = 9, 18  # 9:00–18:00
RESPONSE_SLA_HOURS = 24  # 响应时效（eBay 要求 24 小时内回复）
FOLLOW_UP_HOURS = 48
ESCALATION_DAYS = 7
RULES_VERSION = "2026-11-10d"  # 口径改了就改这里：基线快照在原始消息还完整时按新口径重算
SYSTEM_TYPE = "FROM_EBAY"
BANDS = {"work": "工作时段（周一至周五 9:00–18:00）", "night": "夜间（工作日非工作时段）", "weekend": "周末"}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 沟通后升级算哪些 eBay 售后：咨询（未收到货）、纠纷全部算；退货只算商品或发货出了问题的原因，
# 买家自己的原因（不想要了、买错了、尺寸不对、找到更便宜的……）不算；付款纠纷不算欺诈、交易问题
ESCALATION_KINDS = ("INQUIRY", "CASE", "RETURN", "PAYMENT_DISPUTE")
RETURN_REASONS = {"DEFECTIVE_ITEM", "ARRIVED_DAMAGED", "NOT_AS_DESCRIBED", "MISSING_PARTS", "ORDERED_DIFFERENT_ITEM", "DOES_NOT_FIT"}
DISPUTE_REASONS = {"ITEM_NOT_RECEIVED", "SIGNIFICANTLY_NOT_AS_DESCRIBED", "CREDIT_NOT_PROCESSED"}

# 只是致谢 / 结束语（多语言）：没有否定词、没有问号、很短，并且命中致谢说法
_THANKS = re.compile(
    r"\b(thanks?|thank you|thank u|thanks a lot|many thanks|thx|ty|cheers|great|perfect|awesome|got it|received|all good|ok(ay)?|"
    r"danke|dank|besten dank|vielen dank|herzlichen dank|vielen herzlichen dank|tausend dank|danke sehr|dankesch[öo]n|"
    r"alles klar|super|prima|perfekt|erhalten|bekommen|"
    r"merci|merci beaucoup|parfait|re[çc]u|grazie|grazie mille|perfetto|ricevuto|gracias|muchas gracias|perfecto|recibido|vale)\b"
    r"|谢谢|多谢|收到了|好的|已收到", re.I)
# 投诉 / 诉求：带了 received / ok / gracias 也不是致谢（“I received the wrong part”“Ok, I want a refund”）
_COMPLAINT = re.compile(
    r"\b(refund\w*|money back|wrong|broken|damaged|defect\w*|faulty|not working|doesn'?t (fit|work)|does not (fit|work)|"
    r"missing|return\w*|complain\w*|cancel\w*|"
    r"r[üu]ckerstatt\w*|erstatt\w*|geld zur[üu]ck|falsch\w*|defekt\w*|kaputt\w*|besch[äa]digt\w*|passt nicht|"
    r"funktioniert nicht|fehlt|reklamation|r[üu]cksend\w*|zur[üu]ck(geben|schicken|senden)|storn\w*|"
    r"rembours\w*|cass[ée]e?s?|endommag\w*|d[ée]fectueu\w*|manquant\w*|annul\w*|"
    r"rimbors\w*|rott[oa]|danneggiat\w*|difettos\w*|mancant\w*|reso|annull\w*|"
    r"devoluci[oó]n|reembols\w*|roto|rota|da[ñn]ad\w*|defectuos\w*|falta|cancel\w*)\b|退款|退货|坏了|错了|不对|少了", re.I)
_NEGATION = re.compile(r"\b(not|no|never|nicht|kein\w*|noch|pas|non|ne|nada|todav[ií]a|still|but|aber|mais|ma|pero)\b|没|不|还", re.I)


def is_thanks_only(text: str | None, intent: str | None = None) -> bool:
    """这条来信只是致谢 / 结束语（不用当成追问）。有 AI 判的意图就用意图，没有时用关键词兜底。"""
    if intent:
        return intent == "thanks_closing"
    t = " ".join((text or "").split())
    if not t or len(t) > 120 or "?" in t or "？" in t:
        return False
    return bool(_THANKS.search(t)) and not _NEGATION.search(t) and not _COMPLAINT.search(t)


# 店铺回复在请买家补资料（多语言）：要有“资料”词，并且是请求（请 / 能否 / 发送…或带问号）
_INFO = re.compile(
    r"\b(vin|fin|chassis|vehicle identification|fahrgestell\w*|fahrzeug-?ident\w*|kba|hsn|tsn|schl[üu]sselnummer|"
    r"fahrzeugschein|zulassungsbescheinigung|registration (document|certificate)|carte grise|libretto|permiso de circulaci[oó]n|"
    r"n[uú]mero de (bastidor|chasis)|num[eé]ro de ch[aâ]ssis|telaio|"
    r"photos?|pictures?|images?|videos?|fotos?|bilder?|bild|vid[eé]o|immagin\w*|imag[eé]n\w*|"
    r"order (number|id)|bestellnummer|auftragsnummer|num[eé]ro de commande|numero d'ordine|n[uú]mero de pedido|"
    r"e-?mail|mail-?adresse|adresse e-?mail|indirizzo e-?mail|correo|"
    r"model year|year of manufacture|baujahr|erstzulassung|ann[ée]e|anno|a[ñn]o|make and model|car model|modell|mod[eè]le|modello|modelo|"
    r"part number|teilenummer|oe-?nummer|oe number|r[ée]f[ée]rence)\b|车架号|照片|视频|订单号|邮箱|年款|车型", re.I)
_ASK = re.compile(
    r"\?|？|\b(please|kindly|could you|can you|would you|send|provide|share|let us know|"
    r"bitte|senden|schicken|zusenden|teilen sie|k[öo]nnten sie|k[öo]nnen sie|"
    r"merci de|pourriez|pouvez|envoy\w*|"
    r"per favore|potrebbe|pu[oò]|invia\w*|"
    r"por favor|podr[ií]a|puede|env[ií]\w*)\b|请|麻烦|发一下|提供", re.I)


# 不是在向买家要资料：引用买家已经给的、我们已经发了、请买家查收、告诉买家
_NOT_ASK = re.compile(
    r"\b(you (have )?provided|you sent|you gave|according to (the|your)|based on (the|your)|we (have )?sent|has been sent|"
    r"i (have )?sent|already sent|please (check|find)|attached|"
    r"von ihnen (angegeben|genannt|gesendet|geschickt|mitgeteilt)\w*|ihre[nm]? angegeben\w*|aufgrund der|anhand der|"
    r"(haben|habe) (ihnen|wir) .{0,30}(gesendet|geschickt)|bereits (gesendet|geschickt)|im anhang|anbei|"
    r"ihnen .{0,40}mitteilen|"
    r"que vous (avez|nous avez) (fourni|envoy)\w*|selon (le|la|votre)|ci-joint|"
    r"che (ci )?ha (fornito|inviato)|in allegato|"
    r"que (nos )?(proporcion|envi)\w*|seg[uú]n (el|la|su)|adjunto)\b"
    r"|您提供的|根据您|已发|已经发|请查收|查收|告诉您|附件", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|[\n;；]")


def is_info_request(text: str | None) -> bool:
    """店铺这次回复是在请买家补资料（VIN、照片、订单号、车型年款、邮箱等）：同一句里既有请求又有资料词，
    且这句不是“您提供的 / 已发给您 / 请查收 / 告诉您”这类说法。买家下一轮补发过来不算追问。"""
    for sentence in _SENT_SPLIT.split(text or ""):
        t = " ".join(sentence.split())
        if t and _INFO.search(t) and _ASK.search(t) and not _NOT_ASK.search(t):
            return True
    return False


# 买家这一轮是在补资料（或说明提供不了）：VIN、邮箱、年款 / 较长号码、资料词、图片说明；没有文字（只发了图片）也算
_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_NUMBER = re.compile(r"\b(19|20)\d{2}\b|\d{5,}")
_CANT = re.compile(r"\b(can'?t|cannot|unable|no longer|don'?t have|kann (ich )?nicht|nicht mehr|weggeworfen|habe keine?|"
                   r"ne peux pas|non posso|non ho|no puedo|ya no|no tengo)\b|没法|没办法|扔了|拍不了|没有了|不能", re.I)
_MEDIA_HINT = re.compile(r"\b(attached|see (the )?(photo|picture|image)s?|anbei|im anhang|siehe (foto|bild)|ci-joint|in allegato|adjunto)\b"
                         r"|附件|见图|图片", re.I)


def supplies_info(text: str | None) -> bool:
    t = " ".join((text or "").split())
    if not t:
        return True  # 只发了图片 / 附件
    return bool(_VIN.search(t) or _EMAIL.search(t) or _NUMBER.search(t) or _INFO.search(t) or _CANT.search(t)
                or _MEDIA_HINT.search(t))


def band(at: datetime) -> str:
    """到达时段（at 为 UTC，无时区当 UTC）：work / night / weekend。"""
    local = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(TZ)
    if local.weekday() not in WORK_DAYS:
        return "weekend"
    return "work" if WORK_START <= local.hour < WORK_END else "night"


@dataclass
class Msg:
    at: datetime  # UTC，无时区
    role: str  # BUYER / SELLER
    ack: bool = False
    text: str = ""
    intent: str | None = None


@dataclass
class Conv:
    key: str
    store_id: str
    buyer: str
    msgs: list[Msg] = field(default_factory=list)


@dataclass
class Round:
    conv: str
    store_id: str
    buyer: str
    start: datetime
    reply_at: datetime | None
    thanks_only: bool
    next_start: datetime | None = None  # 同一会话下一轮开始的时间
    next_thanks_only: bool = False
    info_request: bool = False  # 这轮的店铺回复是在请买家补资料
    buyer_text: str = ""  # 这一轮买家说的话（下一轮判断是不是在补资料用）
    next_supplies_info: bool = True  # 下一轮买家是不是在补资料


def build_rounds(convs: list[Conv]) -> list[Round]:
    out: list[Round] = []
    for c in convs:
        msgs = sorted(c.msgs, key=lambda m: m.at)
        rounds: list[Round] = []
        cur: Round | None = None
        thanks: list[bool] = []
        for m in msgs:
            if m.role == "BUYER":
                if cur is None:
                    cur, thanks = Round(c.key, c.store_id, c.buyer, m.at, None, False), []
                thanks.append(is_thanks_only(m.text, m.intent))
                cur.buyer_text = f"{cur.buyer_text}\n{m.text or ''}".strip()
            elif m.role == "SELLER" and not m.ack and cur is not None:
                cur.reply_at, cur.thanks_only = m.at, all(thanks)
                cur.info_request = is_info_request(m.text)
                rounds.append(cur)
                cur = None
        if cur is not None:
            cur.thanks_only = all(thanks)
            rounds.append(cur)
        for a, b in zip(rounds, rounds[1:]):
            a.next_start, a.next_thanks_only = b.start, b.thanks_only
            a.next_supplies_info = supplies_info(b.buyer_text)
        out.extend(rounds)
    return out


def _med(v):
    return int(median(v)) if v else None


def _p90(v):
    if not v:
        return None
    s = sorted(v)
    return int(s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))])


def _pct(n, d):
    return round(n / d, 4) if d else None


def compute(rounds: list[Round], cases: dict[tuple[str, str], list[datetime]], now: datetime,
            msgs_buyer: list[tuple[str, datetime]], days: int) -> dict:
    """rounds：要统计的来信轮次；cases：(店铺, 买家小写) → 算升级的 eBay 售后开启时间；now：观察截止（UTC）；
    msgs_buyer：(店铺, 时间) 买家消息，算消息量；days：统计天数（算日均）。"""
    sla, fu, esc = timedelta(hours=RESPONSE_SLA_HOURS), timedelta(hours=FOLLOW_UP_HOURS), timedelta(days=ESCALATION_DAYS)
    by_band: dict[str, list[float]] = defaultdict(list)
    band_rounds = Counter()
    on_time = late = sla_watch = 0
    fu_yes = fu_no = fu_watch = 0
    esc_yes = esc_no = esc_watch = 0
    ns_good = ns_bad = ns_watch = 0
    backlog = backlog_pending = off_hours = 0
    # 一张售后只算一轮：归到开启前最近的那一轮回复（且在它回复后 esc 天内）
    by_buyer: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(rounds):
        if r.reply_at is not None:
            by_buyer[(r.store_id, r.buyer.lower())].append(i)
    hit: set[int] = set()
    for key, idx in by_buyer.items():
        for t in cases.get(key, []):
            before = [i for i in idx if rounds[i].reply_at < t]
            if before:
                last = max(before, key=lambda i: rounds[i].reply_at)
                if t <= rounds[last].reply_at + esc:
                    hit.add(last)
    for i, r in enumerate(rounds):
        b = band(r.start)
        band_rounds[b] += 1
        if b != "work":
            off_hours += 1
        # 按时
        if r.reply_at is not None:
            secs = max(0.0, (r.reply_at - r.start).total_seconds())
            by_band[b].append(secs)
            ok_time = secs <= sla.total_seconds()
            on_time += ok_time
            late += not ok_time
            if b != "work" and band(r.reply_at) == "work":
                backlog += 1
        elif r.start + sla <= now:
            ok_time = False
            late += 1
        else:
            ok_time = None
            sla_watch += 1
        if r.reply_at is None and b != "work":
            backlog_pending += 1
        # 追问
        followed = escalated = None
        if r.reply_at is not None:
            excused = r.info_request and r.next_supplies_info  # 我们请买家补资料、买家确实在补：不算追问
            if r.next_start is not None and r.next_start <= r.reply_at + fu and not r.next_thanks_only and not excused:
                followed = True
            elif excused and r.next_start is not None and r.next_start <= r.reply_at + fu:
                followed = False  # 我们请买家补资料，买家补发过来：不算追问
            elif r.reply_at + fu <= now:
                followed = False
            if followed is True:
                fu_yes += 1
            elif followed is False:
                fu_no += 1
            else:
                fu_watch += 1
            # 升级
            if i in hit:
                escalated = True
            elif r.reply_at + esc <= now:
                escalated = False
            if escalated is True:
                esc_yes += 1
            elif escalated is False:
                esc_no += 1
            else:
                esc_watch += 1
        # 北极星：没按时（含超时没回）= 不达标；按时的要等追问、升级窗口都走完
        if ok_time is False or followed is True or escalated is True:
            ns_bad += 1
        elif ok_time is True and followed is False and escalated is False:
            ns_good += 1
        else:
            ns_watch += 1

    vol_store, vol_hour, vol_week = Counter(), Counter(), Counter()
    for store, at in msgs_buyer:
        local = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(TZ)
        vol_store[store] += 1
        vol_hour[local.hour] += 1
        vol_week[local.weekday()] += 1
    replied = sum(1 for r in rounds if r.reply_at is not None)
    return {
        "rounds": len(rounds), "replied_rounds": replied,
        "bands": [{"key": k, "label": BANDS[k], "rounds": band_rounds[k], "replied": len(by_band[k]),
                   "median_seconds": _med(by_band[k]), "p90_seconds": _p90(by_band[k])} for k in BANDS],
        "on_time": {"rate": _pct(on_time, on_time + late), "on_time": on_time, "late": late, "observing": sla_watch,
                    "sla_hours": RESPONSE_SLA_HOURS},
        "follow_up": {"rate": _pct(fu_yes, fu_yes + fu_no), "followed": fu_yes, "not_followed": fu_no, "observing": fu_watch,
                      "hours": FOLLOW_UP_HOURS},
        "escalation": {"rate": _pct(esc_yes, esc_yes + esc_no), "escalated": esc_yes, "not_escalated": esc_no,
                       "observing": esc_watch, "days": ESCALATION_DAYS},
        "north_star": {"rate": _pct(ns_good, ns_good + ns_bad), "good": ns_good, "bad": ns_bad, "observing": ns_watch},
        "night_backlog": {"count": backlog, "pending": backlog_pending, "off_hours_rounds": off_hours,
                          "rate": _pct(backlog, off_hours), "per_week": round(backlog / days * 7, 2) if days else None},
        "volume": {"total": len(msgs_buyer), "per_day": round(len(msgs_buyer) / days, 2) if days else None,
                   "by_store": [{"key": k, "count": n} for k, n in vol_store.most_common()],
                   "by_hour": [{"hour": h, "count": vol_hour[h]} for h in range(24)],
                   "by_weekday": [{"key": WEEKDAYS[d], "count": vol_week[d]} for d in range(7)]},
    }


# ------------------------------------------------------------------ 数据源
def escalation_cases(db: Session, stores: list[str] | None) -> dict[tuple[str, str], list[datetime]]:
    q = select(EbayCase.store_id, EbayCase.buyer, EbayCase.kind, EbayCase.reason, EbayCase.opened_at).where(
        EbayCase.kind.in_(ESCALATION_KINDS), EbayCase.opened_at.is_not(None), EbayCase.buyer.is_not(None))
    if stores is not None:
        q = q.where(EbayCase.store_id.in_(stores))
    out: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for store, buyer, kind, reason, at in db.execute(q):
        if kind == "RETURN" and reason not in RETURN_REASONS:
            continue
        if kind == "PAYMENT_DISPUTE" and reason not in DISPUTE_REASONS:
            continue
        out[(store, buyer.lower())].append(at)
    return out


def live_conversations(db: Session, stores: list[str] | None, lo: datetime, hi: datetime) -> list[Conv]:
    """上线后：在 [lo, hi) 有买家来信的会话，带上这之后的全部消息（算回复、追问要用）；买家来信带 AI 判的意图。"""
    cq = select(Conversation.id).where(Conversation.conversation_type != SYSTEM_TYPE)
    if stores is not None:
        cq = cq.where(Conversation.store_id.in_(stores))
    ids = sorted(set(db.scalars(select(Message.conversation_id).where(
        Message.sender_role == "BUYER", Message.direction == "IN", Message.sent_at >= lo, Message.sent_at < hi,
        Message.conversation_id.in_(cq)))))
    convs: dict[int, Conv] = {}
    rows = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for c in db.execute(select(Conversation.id, Conversation.store_id, Conversation.buyer_key).where(Conversation.id.in_(part))):
            convs[c.id] = Conv(str(c.id), c.store_id, c.buyer_key or "")
        rows += db.execute(select(Message.conversation_id, Message.ebay_message_id, Message.sender_role, Message.direction,
                                  Message.auto_kind, Message.sent_at, Message.body)
                           .where(Message.conversation_id.in_(part), Message.sent_at >= lo - timedelta(days=1),
                                  Message.sender_role.in_(("BUYER", "SELLER")))).all()
    intents = _buyer_intents(db, [r.ebay_message_id for r in rows if r.sender_role == "BUYER"])
    for r in rows:
        if r.sender_role == "BUYER" and r.direction != "IN" or r.sender_role == "SELLER" and r.direction != "OUT":
            continue
        convs[r.conversation_id].msgs.append(Msg(r.sent_at, r.sender_role, r.auto_kind == "ACK" or is_ack(r.ebay_message_id),
                                                 r.body or "", intents.get(r.ebay_message_id)))
    return list(convs.values())


def _buyer_intents(db: Session, ebay_ids: list[str]) -> dict[str, str]:
    """买家来信 → 覆盖它的回复任务最后一版 AI 草稿的主要意图（判断“只是致谢”用）。"""
    out: dict[str, str] = {}
    for i in range(0, len(ebay_ids), 500):
        part = ebay_ids[i:i + 500]
        src = db.execute(select(ReplyJobSource.source_message_id, ReplyJobSource.job_id)
                         .where(ReplyJobSource.source_message_id.in_(part))).all()
        jobs = {j for _, j in src}
        latest: dict[int, str] = {}
        if jobs:
            for job_id, intent in db.execute(select(Draft.job_id, Draft.intent).where(
                    Draft.job_id.in_(jobs), Draft.author == "agent", Draft.intent.is_not(None)).order_by(Draft.id)):
                latest[job_id] = intent
        for mid, job_id in src:
            if job_id in latest:
                out[mid] = latest[job_id]
    return out


def baseline_available(db: Session) -> bool:
    """基线表在不在（表由 1 号的补拉脚本 / models.py 定义，线上库要重启后才建好）。"""
    from .models import BaselineBatch, BaselineConversation, BaselineMessage

    insp = inspect(db.get_bind())  # 表名按模型取（MySQL 上带 cs_ 前缀）
    return all(insp.has_table(m.__tablename__) for m in (BaselineBatch, BaselineConversation, BaselineMessage))


def baseline_status(db: Session) -> tuple[str, tuple[datetime, datetime] | None, list[int]]:
    """基线状态、覆盖时间段、完成的批次号：READY 有完成的批次（DONE / PARTIAL，时间段取最早起点到最晚终点，终点不含）/
    RUNNING 还在采集 / NONE 没采集。"""
    from .models import BaselineBatch

    rows = db.execute(select(BaselineBatch.id, BaselineBatch.period_start, BaselineBatch.period_end)
                      .where(BaselineBatch.status.in_(("DONE", "PARTIAL")))).all()
    rows = [r for r in rows if r.period_start and r.period_end]
    if rows:
        return "READY", (min(r.period_start for r in rows), max(r.period_end for r in rows)), sorted(r.id for r in rows)
    running = db.scalar(select(func.count(BaselineBatch.id)).where(BaselineBatch.status == "RUNNING"))
    return ("RUNNING" if running else "NONE"), None, []


def batch_stores(db: Session, batch_ids: list[int]) -> dict[int, set[str]]:
    """每个批次补拉了哪些店铺（baseline_batches.stores）。"""
    from .models import BaselineBatch

    if not batch_ids:
        return {}
    return {i: set(st or []) for i, st in db.execute(select(BaselineBatch.id, BaselineBatch.stores)
                                                      .where(BaselineBatch.id.in_(batch_ids)))}


def baseline_message_counts(db: Session, lo: datetime, hi: datetime) -> dict[str, int]:
    """基线时间段内每个店铺的消息条数（* = 合计）：和快照记下的条数对比，判断原始消息有没有被清理。"""
    from .models import BaselineConversation, BaselineMessage

    out = {store: n for store, n in db.execute(
        select(BaselineConversation.store_id, func.count(BaselineMessage.id))
        .join(BaselineMessage, BaselineMessage.conversation_id == BaselineConversation.id)
        .where(BaselineMessage.sent_at >= lo, BaselineMessage.sent_at < hi, BaselineConversation.conversation_type != SYSTEM_TYPE)
        .group_by(BaselineConversation.store_id))}
    out["*"] = sum(out.values())
    return out


BACKFILL_ORIGIN = "BASELINE_BACKFILL"  # 1 号按基线时间段补拉的售后单：detail.origin


def case_coverage(db: Session, stores: list[str] | None, lo: datetime, hi: datetime) -> dict[str, dict]:
    """售后数据覆盖到哪里（算基线升级率用）：每个店铺算升级的售后（咨询 / 纠纷 / 退货 / 付款纠纷）最早的开启时间、
    有没有基线补拉进来的单、基线窗口（基线起点到终点后 7 天）里有几张——条数变了说明补拉过，快照要重算。"""
    q = select(EbayCase.store_id, EbayCase.opened_at, EbayCase.detail).where(
        EbayCase.kind.in_(ESCALATION_KINDS), EbayCase.opened_at.is_not(None))
    if stores is not None:
        q = q.where(EbayCase.store_id.in_(stores))
    out: dict[str, dict] = {}
    end = hi + timedelta(days=ESCALATION_DAYS)
    for store, at, detail in db.execute(q):
        c = out.setdefault(store, {"earliest": at, "backfilled": False, "in_window": 0})
        c["earliest"] = min(c["earliest"], at)
        c["backfilled"] = c["backfilled"] or (detail or {}).get("origin") in (BACKFILL_ORIGIN, "CASE_BACKFILL_90D")
        c["in_window"] += lo <= at < end
    return out


def cases_incomplete(coverage: dict[str, dict], stores: list[str], lo: datetime) -> list[str]:
    """售后数据覆盖不到基线起点的店铺：最早的售后晚于基线起点、且没有基线补拉进来的单（或一张都没有）。"""
    bad = []
    for s in stores:
        c = coverage.get(s)
        if c is None or (not c["backfilled"] and c["earliest"] > lo):
            bad.append(s)
    return bad


def snapshot_table_ready(db: Session) -> bool:
    from .models import BaselineSnapshot

    return inspect(db.get_bind()).has_table(BaselineSnapshot.__tablename__)


def baseline_conversations(db: Session, stores: list[str] | None, lo: datetime, hi: datetime) -> list[Conv]:
    """上线前基线：baseline_messages + baseline_conversations（只要买家会话；同一条消息表上已唯一）。"""
    from .models import BaselineConversation, BaselineMessage

    q = (select(BaselineConversation.id, BaselineConversation.store_id, BaselineConversation.buyer_key,
                BaselineConversation.conversation_type, BaselineMessage.ebay_message_id, BaselineMessage.sender_role,
                BaselineMessage.sent_at, BaselineMessage.body)
         .join(BaselineMessage, BaselineMessage.conversation_id == BaselineConversation.id)
         .where(BaselineMessage.sent_at >= lo - timedelta(days=1), BaselineMessage.sent_at < hi + timedelta(days=ESCALATION_DAYS)))
    if stores is not None:
        q = q.where(BaselineConversation.store_id.in_(stores))
    convs: dict[int, Conv] = {}
    for cid, store, buyer, ctype, mid, role, at, body in db.execute(q):
        if ctype == SYSTEM_TYPE or role not in ("BUYER", "SELLER") or at is None:
            continue
        conv = convs.setdefault(cid, Conv(f"b{cid}", store, buyer or ""))
        conv.msgs.append(Msg(at, role, is_ack(mid), body or "", None))
    return list(convs.values())
