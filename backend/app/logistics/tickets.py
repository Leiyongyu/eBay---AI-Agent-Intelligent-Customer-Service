"""谷仓查件单同步（只读拉取）：每隔 LOGISTICS_TICKET_SYNC_INTERVAL_MINUTES 分钟拉最近 LOGISTICS_TICKET_SYNC_DAYS 天的查件单
（含客服在谷仓后台手动建的），按订单号定位店铺、买家和会话。谷仓新回复的：附件（如 POD）一律存进售后附件，
按规则把回复归类（OUTCOMES：已催派、已给 POD、已妥投、在自取点 → 自动回复；丢失、退回、破损 → 需判断补发；
地址问题、妥投未收到、看不懂 → 需人工审核），AI 按买家语言起草消息。开了 LOGISTICS_TICKET_AUTO_SEND 时自动回复的
直接连同附件发给买家（目前为模拟发送；买家还没有会话的新建一个）；要人工的把会话标成待售后、打“物流异常退回后需要重发”或
“物流异常”标记（需补发的附上补发库存），开了 LOGISTICS_TICKET_SEND_NEEDS_HUMAN 时也照样告诉买家查件结果，
看不懂的不发。谷仓对同一张查件单再次回复时按新回复重新处理。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import usage as usage_log
from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import (Aftersales, AftersalesFile, AuditEvent, Conversation, LogisticsTicket, Message, Store,
                      TrackingNumber)
from ..prompts import load_prompt
from ..sending.service import SIMULATED_PREFIX
from .assess import CN
from .goodcang import GoodcangClient, GoodcangError

log = logging.getLogger(__name__)
ACTOR = "logistics_tickets"
EBAY_ORDER = re.compile(r"\d{2}-\d{5}-\d{5}")
SEND_ACTOR = "AI（查件结果）"

# ---------------------------------------------------------------- 谷仓回复归类（规则判定；模型只负责写给买家的话）
RESHIP, REVIEW, AUTO = "需判断补发", "需人工审核", "自动回复"
OUTCOMES = {  # 类别 → (中文说明, 处理方式)
    "LOST": ("包裹疑似丢失 / 可理赔", RESHIP),
    "DAMAGED": ("包裹破损 / 空包", RESHIP),
    "RETURNED": ("包裹已退回或无法再派", RESHIP),
    "ADDRESS": ("地址有问题 / 无法派送，需要给处理意见", REVIEW),
    "DELIVERED_DISPUTE": ("承运商确认妥投（或已给 POD），买家反馈没收到", REVIEW),
    "ACTION": ("拦截 / 改派类查件，需要人工决定", REVIEW),
    "POD_MISSING": ("回复说已提供 POD，但附件没存下来", REVIEW),
    "POD": ("已提供签收证明（POD）", AUTO),
    "DELIVERED": ("承运商确认已妥投，请买家核实", AUTO),
    "PICKUP": ("包裹在自取点等待领取", AUTO),
    "IN_TRANSIT": ("已催派 / 运输中，请留意轨迹", AUTO),
    "UNKNOWN": ("回复看不懂或没有对应处理办法", REVIEW),
    "MODEL_FLAGGED": ("AI 认为规则判定与回复不符", REVIEW),
    "OPEN_CASE": ("买家已开 eBay 纠纷，查件结果交人工", REVIEW),
}
# 拿不准怎么回的、买家已开 eBay 纠纷的（给买家的说法会影响 eBay 判定）：不发给买家，直接转人工
NO_SEND = {"UNKNOWN", "MODEL_FLAGGED", "OPEN_CASE"}
CASE_LABELS = {"RETURN": "退货", "INR": "未收到货咨询", "CASE": "纠纷", "CANCEL": "取消请求"}
DISPUTE_TYPES = {"妥投未收到", "查妥投地址"}  # 买家已经说过“显示妥投但没收到”的查件
ACTION_TYPES = {"拦截", "改派"}
# 归类前去掉否定说法：“未退回”不算退回，“未妥投”“妥投失败”不算已妥投，“未断更”不算断更
_NEGATED = re.compile(r"(未|没有|没|并未|不是)(退回|退件|丢失|丢件|破损|损坏|妥投|签收|断更)|(无|没有)(自取|自提)(地址|点)?|"
                      r"(妥投|签收)失败")
_RULES = [  # 按顺序取第一个命中的；“如需理赔……”这类附带的条件句不算丢件，所以弱的丢件说法排在催派之后
    ("LOST", re.compile(r"考虑.{0,6}丢|确认.{0,4}丢|已丢|丢失了|遗失|可以?(提交|申请)理赔|销毁")),
    ("DAMAGED", re.compile(r"破损|损坏|空包|空箱|空盒")),
    ("RETURNED", re.compile(r"退回|退件|返回发件|无法再派")),
    ("ADDRESS", re.compile(r"地址(错误|有误|不全|不完整|不详|无效)|地址无法(访问|投递|派送)|投递失败|信息不(完整|全)|无法(继续)?派送|派送失败|无人(接收|签收|在家)|找不到收件人|拒收|请指示|处理意见|扣关|清关|海关")),
    ("PICKUP", re.compile(r"自取|自提|取件点|待取|等待领取|快递柜|包裹柜")),
    ("POD", re.compile(r"pod|签收证明|获取成功", re.I)),
    ("DELIVERED", re.compile(r"妥投|已签收|已送达|派送完毕")),
    ("IN_TRANSIT", re.compile(r"催|在途|运输中|运输途中|轨迹(有|已)更新|更新到|留意.{0,6}轨迹|关注.{0,6}轨迹|再等")),
    ("LOST", re.compile(r"丢|理赔|索赔|赔偿|久未更新")),
]


def classify(t: LogisticsTicket) -> str:
    """谷仓回复 → 结果类别（见 OUTCOMES）。"""
    text = _NEGATED.sub("", t.reply or "")
    outcome = next((k for k, rx in _RULES if rx.search(text)), None)
    if outcome is None:
        outcome = "POD" if t.attachments and t.type_name in ({"申请POD"} | DISPUTE_TYPES) else "UNKNOWN"
    if t.type_name in ACTION_TYPES and OUTCOMES[outcome][1] == AUTO:
        return "ACTION"
    if t.type_name in DISPUTE_TYPES and outcome in ("POD", "DELIVERED", "UNKNOWN"):
        return "DELIVERED_DISPUTE"
    return outcome


def handling(outcome: str | None) -> str:
    return OUTCOMES.get(outcome or "UNKNOWN", OUTCOMES["UNKNOWN"])[1]
SITE_LANG = {"EBAY_DE": "德语", "EBAY_AT": "德语", "EBAY_FR": "法语", "EBAY_IT": "意大利语", "EBAY_ES": "西班牙语",
             "EBAY_GB": "英语", "EBAY_US": "英语"}


class TicketReply(BaseModel):
    language: str = Field(default="", max_length=16)
    reply_text: str = Field(max_length=3000)
    reply_zh: str = Field(default="", max_length=3000)
    needs_human: bool = False
    reason: str = Field(default="", max_length=300)


def _cn_now() -> datetime:
    return datetime.now(CN).replace(tzinfo=None)


def _upsert(db: Session, row: dict, now: datetime) -> tuple[LogisticsTicket, bool]:
    t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == row["io_code"]))
    new = t is None
    if new:
        t = LogisticsTicket(io_code=row["io_code"])
        db.add(t)
    t.order_code, t.reference_no = row.get("order_code"), (row.get("reference_no") or "").strip() or None
    t.tracking_no = (row.get("tracking_no") or "").strip() or None
    t.type_name, t.io_status, t.io_status_text = row.get("it_type_name"), row.get("io_status"), row.get("io_status_text")
    t.trail_status_text, t.sm_name, t.warehouse_code = row.get("trail_status_text"), row.get("sm_name"), row.get("warehouse_code")
    old_reply, old = t.reply, {a.get("url"): a for a in (t.attachments or [])}
    t.reply = (row.get("customer_service_reply") or "").strip() or None
    t.reject_reason = (row.get("reject_reason") or "").strip() or None
    t.attachments = [{**old.get(a.get("url"), {}), "name": a.get("name"), "url": a.get("url")}
                     for a in row.get("replay_attachment_file_list") or [] if a.get("url")] or None
    t.added_at, t.replied_at = row.get("add_time") or None, row.get("reply_time") or row.get("last_handle_time") or None
    t.synced_at = now
    changed = t.reply != old_reply or any(a["url"] not in old for a in t.attachments or [])
    if not new and changed and (t.suggested_at or t.handled_at):
        # 谷仓又回复了（如先“已催派”，后来给了 POD / 说丢件）：按新回复重新判断、重新回复买家
        t.suggestion = t.suggestion_zh = t.needs_human = t.suggested_at = t.outcome = None
        t.handled_at = t.handled_by = t.send_status = t.send_error = None
    return t, new


def _locate(db: Session, t: LogisticsTicket, locate=None, cache: dict | None = None) -> None:
    """订单号 → 店铺、买家、会话（定位一次后记下，之后只补会话）。同一订单的其他查件单直接复用，不再查 eBay。"""
    from ..aftersales_mail.service import _conversation, locate_order

    if not t.reference_no or not EBAY_ORDER.fullmatch(t.reference_no):
        return  # 谷仓也发其他平台的订单：只有 eBay 订单号才去定位
    if not t.store_id:
        cache = cache if cache is not None else {}
        if t.reference_no not in cache:
            known = db.scalar(select(LogisticsTicket).where(LogisticsTicket.reference_no == t.reference_no,
                                                            LogisticsTicket.store_id.is_not(None)).limit(1))
            cache[t.reference_no] = ((known.store_id, known.buyer_key, known.conversation_id) if known else
                                     (lambda loc: (loc.store_id, loc.buyer, loc.conversation_id) if loc else None)(
                                         (locate or locate_order)(db, t.reference_no)))
        if cache[t.reference_no]:
            t.store_id, t.buyer_key, t.conversation_id = cache[t.reference_no]
    elif not t.conversation_id:
        t.conversation_id = _conversation(db, t.store_id, t.buyer_key)


def _pick_conversation(db: Session, t: LogisticsTicket, client: GoodcangClient, cache: dict) -> None:
    """买家有多个会话时，把查件结果发到这笔订单商品的会话里（订单商品来自谷仓出库单，每个订单只查一次）。"""
    from ..aftersales_mail.service import order_conversation

    n = db.scalar(select(func.count()).select_from(Conversation).where(
        Conversation.store_id == t.store_id, func.lower(Conversation.buyer_key) == t.buyer_key.lower()))
    if n <= 1 and t.conversation_id:
        return
    key = ("items", t.reference_no)
    if key not in cache:
        try:
            cache[key] = [i.get("item_id") for w in client.orders_by_reference(t.reference_no) for i in w.get("items") or []]
        except Exception:  # noqa: BLE001  查不到商品就按原来的会话
            cache[key] = []
    cid = order_conversation(db, t.store_id, t.buyer_key, t.reference_no, cache[key])
    if cid:
        t.conversation_id = cid


def _replied_recently(t: LogisticsTicket, days: int) -> bool:
    if not (t.reply or t.attachments):
        return False
    try:
        when = datetime.strptime((t.replied_at or t.added_at or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True  # 没有时间的按新回复处理
    return _cn_now() - when <= timedelta(days=days)


def _save_attachments(db: Session, t: LogisticsTicket, http: httpx.Client) -> None:
    """谷仓回复的附件（如签收证明）存进售后附件，按店铺 / 订单号_时间戳 存放，30 天自动清理。"""
    from ..aftersales_mail import storage

    if not t.attachments:
        return
    store = db.get(Store, t.store_id) if t.store_id else None
    folder = storage.store_dir(t.store_id, store.ebay_user_id if store else None)
    out = []
    for i, a in enumerate(t.attachments, 1):
        if a.get("file_id"):
            out.append(a)
            continue
        name = a.get("name") or f"查件附件{i}"
        ext = storage.ext_of(name, None) or ".bin"
        status, reason, saved, scan = "STORED", None, None, None
        try:
            if not storage.allowed(ext):
                status, reason = "BLOCKED_TYPE", f"类型 {ext} 不允许"
            else:
                with http.stream("GET", a["url"], timeout=60) as resp:
                    if resp.status_code != 200:
                        raise httpx.HTTPError(f"HTTP {resp.status_code}")
                    saved = storage.save_stream(resp.iter_bytes(65536), folder,
                                                storage.file_name(t.reference_no, utcnow(), i, ext),
                                                resp.headers.get("content-type", "application/octet-stream"))
                scan, note = storage.scan(saved.rel_path)
                if scan == "THREAT":
                    status, reason = "INFECTED", note
        except storage.FileTooLarge:
            status, reason = "TOO_LARGE", "超过单个文件大小上限"
        except httpx.HTTPError as e:
            status, reason = "DOWNLOAD_FAILED", f"下载失败：{e}"[:300]
        f = AftersalesFile(mail_id=None, store_id=t.store_id, order_id=t.reference_no, conversation_id=t.conversation_id,
                           source="GOODCANG_TICKET", original_name=name[:300], size_bytes=saved.size if saved else None,
                           content_type=(saved.content_type[:100] if saved else None), sha256=saved.sha256 if saved else None,
                           rel_path=saved.rel_path if saved else None, link_url=a["url"][:1000], status=status,
                           status_reason=reason or f"谷仓查件单 {t.io_code}（{t.type_name}）的回复附件", scan_result=scan)
        db.add(f)
        db.flush()
        out.append({**a, "file_id": f.id})
    t.attachments = out


def _context(db: Session, t: LogisticsTicket) -> tuple[str, str]:
    msgs = db.scalars(select(Message).where(Message.conversation_id == t.conversation_id)
                      .order_by(Message.conversation_seq.desc()).limit(10)).all()[::-1]
    conv = "\n".join(f"[{m.sender_role} {m.sent_at:%Y-%m-%d}] {(m.body or '')[:600]}" for m in msgs)
    if not any(m.sender_role == "BUYER" for m in msgs):
        c = db.get(Conversation, t.conversation_id)
        site = c.marketplace_id if c else None
        conv = (conv + "\n" if conv else "") + (f"（买家还没有来信，这是我们主动通知；站点 {site}，"
                                                f"用{SITE_LANG.get(site or '', '英语')}写）")
    rec = db.scalar(select(TrackingNumber).where(TrackingNumber.number == (t.tracking_no or "")))
    tr = rec.track or {} if rec else {}
    tracking = {"carrier": tr.get("carrier"), "tracking_number": t.tracking_no, "status": tr.get("status_label"),
                "latest_event": tr.get("latest_event")} if rec else {"tracking_number": t.tracking_no}
    return conv, str(tracking)


def _stored_files(db: Session, t: LogisticsTicket) -> list[AftersalesFile]:
    files = [db.get(AftersalesFile, a["file_id"]) for a in t.attachments or [] if a.get("file_id")]
    return [f for f in files if f is not None and f.status == "STORED"]


def _find_cases(store_id: str, order_id: str) -> list[dict]:
    """这笔订单在 eBay 上的退货 / 未收到货咨询 / 纠纷 / 取消（和工具 get_case_status 同一个数据源）；非 eBay 店铺返回空。
    测试里替换掉这个函数。"""
    from ..ebay.stores import is_ebay_store
    from ..sources import get_ebay_source

    if not is_ebay_store(store_id):
        return []
    return get_ebay_source().find_cases(store_id, [order_id])


def _open_case(t: LogisticsTicket) -> dict | None:
    """这笔订单有没有没结的 eBay case。查不到（非 eBay 店铺、没有订单号、接口报错）时不拦，返回 None。"""
    if not (t.store_id and t.reference_no):
        return None
    try:
        cases = _find_cases(t.store_id, t.reference_no)
    except Exception as e:  # noqa: BLE001  查不了 case 就按原规则走
        log.warning("查件单 %s 查 eBay case 失败，按原规则处理：%s: %s", t.io_code, type(e).__name__, str(e)[:200])
        return None
    return next((c for c in cases or [] if str(c.get("status") or "").upper() not in ("CLOSED", "RESOLVED")), None)


def _claimed_not_received(db: Session, t: LogisticsTicket) -> bool:
    """这个会话之前的回复任务里识别出过“轨迹已签收但买家说没收到”（TRACKING_DELIVERED_NOT_RECEIVED）。"""
    from ..models import ReplyJob

    if not t.conversation_id:
        return False
    rows = db.scalars(select(ReplyJob.decision_reasons).where(ReplyJob.conversation_id == t.conversation_id)
                      .order_by(ReplyJob.id.desc()).limit(20)).all()
    return any("TRACKING_DELIVERED_NOT_RECEIVED" in (r or []) for r in rows)


def _decide(db: Session, t: LogisticsTicket) -> None:
    """按规则给谷仓回复归类，定下要不要人工；模型的判断不能把规则判成要人工的改成不要。"""
    outcome = classify(t)
    # 员工手动建的“申请POD”，背后其实是买家说已签收但没收到：给 POD 之后还要人工跟进
    if t.type_name == "申请POD" and outcome in ("POD", "DELIVERED") and _claimed_not_received(db, t):
        outcome = "DELIVERED_DISPUTE"
    if outcome == "POD" and not _stored_files(db, t):
        outcome = "POD_MISSING"  # 说了附上 POD 却发不出附件：不能自动发
    case = _open_case(t)
    if case:  # 买家已开 eBay 纠纷：查件结果交人工，不自动发给买家；备注写明原本的类别和 case
        kind = CASE_LABELS.get(case.get("case_type"), case.get("case_type") or "case")
        t.send_error = (f"买家已开 eBay {kind}（{case.get('case_id')}，{case.get('last_action') or case.get('status')}），"
                        f"查件结果原本判为【{OUTCOMES[outcome][0]}】，交人工按纠纷情况回复")[:300]
        outcome = "OPEN_CASE"
    t.outcome, t.needs_human = outcome, handling(outcome) != AUTO


def _suggest(db: Session, t: LogisticsTicket, provider) -> None:
    conv_text, tracking = _context(db, t)
    files = [f.original_name for f in _stored_files(db, t)]
    label, how = OUTCOMES[t.outcome]
    ticket = (f"查件类型：{t.type_name}\n谷仓状态：{t.io_status_text}\n物流状态：{t.trail_status_text}\n"
              f"服务商回复：{t.reply or '（无文字）'}\n随消息发给买家的附件：{'、'.join(files) or '无'}")
    decision = (f"结果类别：{label}\n处理方式：" +
                ("不需要人工，这条消息就是最终答复" if how == AUTO else f"{how}：同事会跟进，消息里只说同事会尽快处理，不承诺补发、退款"))
    msgs = [SystemMessage(content=load_prompt("logistics_ticket_reply")),
            HumanMessage(content=f"<conversation>\n{conv_text}\n</conversation>\n<ticket>\n{ticket}\n</ticket>\n"
                                 f"<decision>\n{decision}\n</decision>\n<tracking>\n{tracking}\n</tracking>")]
    provider_name, model = usage_log.chat_model(get_settings())
    started = time.perf_counter()
    r = provider.structured(msgs, TicketReply)
    usage_log.record(db, "ticket_reply", provider=provider_name, model=model, usage=r.usage, ok=r.parsed is not None,
                     error=r.error, duration_ms=int((time.perf_counter() - started) * 1000), store_id=t.store_id,
                     conversation_id=t.conversation_id)
    if r.parsed:
        t.suggestion, t.suggestion_zh = r.parsed.reply_text.strip(), r.parsed.reply_zh.strip() or None
        if r.parsed.needs_human and not t.needs_human:  # 模型只能把“自动回复”升级成人工，不能反过来
            t.outcome, t.needs_human = "MODEL_FLAGGED", True
            t.send_error = f"AI 认为需要人工：{r.parsed.reason}"[:300]
    else:
        t.needs_human = True  # 没写出消息：转人工
    t.suggested_at = utcnow()


def _ensure_conversation(db: Session, t: LogisticsTicket) -> Conversation | None:
    """买家还没来信时，为这个订单新建一个本地买家会话，用来发查件结果（真实发送接入后改为 eBay 主动消息）。"""
    from ..ingestion.service import get_or_create_conversation

    if t.conversation_id:
        return db.get(Conversation, t.conversation_id)
    if not (t.store_id and t.buyer_key and t.reference_no):
        return None
    store = db.get(Store, t.store_id)
    conv = get_or_create_conversation(db, t.store_id, f"ORDER-{t.reference_no}", t.buyer_key, None,
                                      store.default_marketplace_id if store else "UNKNOWN", "logistics_ticket")
    conv.channel_source = "logistics"  # 真实买家、本系统发起的通知会话：不是测试会话，也不是从 eBay 拉取的
    t.conversation_id = conv.id
    return conv


def _send(db: Session, t: LogisticsTicket, conv: Conversation) -> None:
    """把 AI 起草的查件结果连同附件（妥投照片、签收单等）放进发件箱（延迟发出、可撤回）。合规硬阻断的不发。
    对接了 eBay 的店铺：附件给 eBay 一个公网 HTTPS 地址（sending/media_links，最多 5 个，图片或 PDF）；
    没有公网地址、附件类型 eBay 不收（如视频）、超过 5 个的交人工。"""
    from ..guards.compliance import ComplianceChecker
    from ..models import Store
    from ..sending.channel import is_live_store
    from ..sending.outbox import enqueue

    text = (t.suggestion or "").strip()
    if not text:
        t.send_status, t.send_error = "FAILED", "没有起草出消息"
        return
    check = ComplianceChecker().check(text)
    if check.result == "BLOCK":
        t.send_status, t.send_error = "FAILED", "合规检查不通过，需人工修改后发送"
        return
    media = []
    for f in _stored_files(db, t):
        ctype = f.content_type or ""
        kind = "IMAGE" if ctype.startswith("image/") else ("PDF" if "pdf" in ctype else "DOC")
        media.append({"mediaName": f.original_name, "mediaType": kind, "fileId": f.id})
    ebay_media = None
    if media and is_live_store(db.get(Store, conv.store_id)) and conv.channel_source == "ebay":
        from ..sending.media_links import url_for

        unsupported = [m["mediaName"] for m in media if m["mediaType"] not in ("IMAGE", "PDF")]
        urls = [url_for(m["fileId"], m["mediaName"]) for m in media]
        if unsupported or None in urls or len(media) > 5:
            why = ("附件类型 eBay 不收：" + "、".join(unsupported)) if unsupported else (
                "附件超过 5 个" if len(media) > 5 else "没有附件公网地址（PUBLIC_MEDIA_BASE_URL / EBAY_NOTIFY_ENDPOINT）")
            t.send_status, t.send_error = "NEEDS_HUMAN", f"查件结果带附件，{why}：请人工发给买家"
            return
        ebay_media = [{"mediaName": m["mediaName"] or "attachment", "mediaType": m["mediaType"], "mediaUrl": u}
                      for m, u in zip(media, urls)]
    ob = enqueue(db, conv, text, actor=SEND_ACTOR, source="ticket", zh=t.suggestion_zh,
                 meta={"ticket_id": t.id, "media": media or None, "ebay_media": ebay_media})
    now = utcnow()
    if ob.state != "SENT":
        t.send_status, t.send_error = "QUEUED", None  # 发出后由发件箱回写 SENT
    t.handled_at, t.handled_by = now, SEND_ACTOR
    db.add(AuditEvent(actor=SEND_ACTOR, action="TICKET_REPLY_QUEUED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"io_code": t.io_code, "outbound_id": ob.id, "attachments": len(media),
                              "compliance": check.result, "source": "logistics_ticket"}))


RESHIP_TYPE, LOGISTICS_TYPE = "物流问题需判断补发", "物流异常/未收到"


def _result_line(t: LogisticsTicket) -> str:
    line = f"谷仓查件单 {t.io_code}（{t.type_name}）已回复【{OUTCOMES[t.outcome or 'UNKNOWN'][0]}】：{(t.reply or '见附件')[:300]}"
    if t.outcome == "OPEN_CASE" and t.send_error:
        line += f"（{t.send_error}）"
    return line


def _note(conv: Conversation, line: str) -> None:
    """已在售后处理中的会话：把查件结果补进备注（新的放前面），同一条不重复写。"""
    if line[:80] not in (conv.aftersales_note or ""):
        conv.aftersales_note = "；".join(x for x in (line, conv.aftersales_note) if x)[:1000]
        conv.aftersales_updated_at = utcnow()


def _mark(db: Session, t: LogisticsTicket, client: GoodcangClient | None = None) -> None:
    """查件结果要人工：会话标“待售后处理”（已在售后中的补进备注；需补发的类型升级成需判断补发），
    并打标记“物流异常退回后需要重发”（需补发）或“物流异常”，需补发的附上原发货仓补发库存。"""
    from .. import auto_tags

    conv = db.get(Conversation, t.conversation_id) if t.conversation_id else None
    if not conv:
        return
    reship = handling(t.outcome) == RESHIP
    line = _result_line(t)
    if reship and client is not None and t.reference_no:
        from .followup import reship_stock

        try:
            line += "；" + reship_stock(client, t.reference_no)["summary"]
        except Exception as e:  # noqa: BLE001  查不到库存也要照常标记
            line += f"；补发库存查询失败：{e if isinstance(e, GoodcangError) else type(e).__name__}"
    auto_tags.apply_names(db, conv, ["物流异常退回后需要重发" if reship else "物流异常"],
                          {"io_code": t.io_code, "outcome": t.outcome})
    if conv.aftersales_status in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
        _note(conv, line)
        if reship:
            conv.aftersales_type = RESHIP_TYPE
        return
    now = utcnow()
    conv.aftersales_status = Aftersales.PENDING
    conv.aftersales_type = RESHIP_TYPE if reship else LOGISTICS_TYPE
    conv.aftersales_note = line[:1000]
    conv.aftersales_marked_by, conv.aftersales_marked_at, conv.aftersales_handled_by = "AI（查件结果）", now, None
    conv.aftersales_updated_at = now
    db.add(AuditEvent(actor=ACTOR, action="AFTERSALES_AUTO_MARKED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"io_code": t.io_code, "type": t.type_name, "outcome": t.outcome}))


def sync(db: Session, *, client: GoodcangClient | None = None, provider=None, locate=None,
         http: httpx.Client | None = None, suggest_days: int | None = None) -> dict:
    s = get_settings()
    suggest_days = suggest_days or s.logistics_ticket_reply_days
    client = client or GoodcangClient(s)
    end = _cn_now()
    start = end - timedelta(days=min(186, max(1, s.logistics_ticket_sync_days)))
    fmt = "%Y-%m-%d %H:%M:%S"
    now = utcnow()
    stats = {"tickets": 0, "new": 0, "located": 0, "suggested": 0, "needs_human": 0, "sent": 0, "errors": []}
    page, seen = 1, []
    while True:
        rows, total = client.ticket_list(start.strftime(fmt), end.strftime(fmt), page=page)
        for row in rows:
            if not row.get("io_code"):
                continue
            t, new = _upsert(db, row, now)
            stats["tickets"] += 1
            stats["new"] += new
            seen.append(t)
        if not rows or page * 200 >= total:
            break
        page += 1
    db.flush()
    if provider is None:
        from ..providers.chat import get_chat_provider

        provider = get_chat_provider()
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    cache: dict = {}
    for t in seen:
        try:
            _locate(db, t, locate, cache)
            stats["located"] += bool(t.store_id)
            fresh = not t.handled_at and not t.suggested_at and _replied_recently(t, suggest_days)
            if fresh and t.store_id and t.buyer_key:
                _pick_conversation(db, t, client, cache)
            if fresh and s.logistics_ticket_auto_send:
                conv = _ensure_conversation(db, t)
            else:
                conv = db.get(Conversation, t.conversation_id) if t.conversation_id else None
            if fresh and t.store_id:
                _save_attachments(db, t, http)  # 附件（POD 等）一律存下来，有没有买家会话都能在查件单页看
            if fresh and conv is None and t.store_id:
                t.send_status, t.send_error = "NO_BUYER", "没找到订单买家，无法通知"
            if fresh and conv is not None:
                _decide(db, t)
                if provider is not None:
                    _suggest(db, t, provider)
                    stats["suggested"] += 1
                else:
                    t.needs_human = True
                label, how = OUTCOMES[t.outcome]
                try:  # 异常包裹跟进表：写回复结论和下一步
                    from ..parcel_auto import on_ticket_result

                    with db.begin_nested():
                        on_ticket_result(db, t, label, how)
                except Exception:  # noqa: BLE001
                    log.exception("异常包裹跟进补写失败 ticket=%s", t.io_code)
                if t.needs_human:
                    stats["needs_human"] += 1
                    # 看不懂的回复不发；其他要人工的按配置照样告诉买家查件结果（不承诺退款补发），同事在售后里跟进
                    if s.logistics_ticket_auto_send and s.logistics_ticket_send_needs_human and t.outcome not in NO_SEND:
                        _send(db, t, conv)
                        stats["sent"] += t.send_status in ("SENT", "QUEUED")
                    else:
                        t.send_status = "NEEDS_HUMAN"
                        t.send_error = t.send_error or f"{how}：{label}，没有自动发送"
                    _mark(db, t, client)
                elif s.logistics_ticket_auto_send:
                    _send(db, t, conv)
                    if t.send_status in ("SENT", "QUEUED"):  # QUEUED = 已进发件箱，到点发出
                        stats["sent"] += 1
                        if conv.aftersales_status in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
                            _note(conv, _result_line(t) + "（已自动回复买家）")
                    else:  # 发送失败（合规不过、没写出消息）：转人工审核
                        t.needs_human = True
                        stats["needs_human"] += 1
                        _mark(db, t, client)
            db.flush()
        except Exception as e:  # noqa: BLE001  单张失败不影响其他
            log.exception("查件单 %s 处理失败", t.io_code)
            stats["errors"].append(f"{t.io_code}: {type(e).__name__}")
    db.add(AuditEvent(actor=ACTOR, action="LOGISTICS_TICKET_SYNC", detail={k: v for k, v in stats.items() if k != "errors"}
                      | {"errors": stats["errors"][:20]}))
    return stats


class LogisticsTicketWorker:
    """每隔 LOGISTICS_TICKET_SYNC_INTERVAL_MINUTES 分钟同步一次查件单（设为 0 时每天 LOGISTICS_TICKET_SYNC_TIME 一次，
    没开机的启动后补跑）；谷仓没配置时不启动。"""

    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict | None = None
        self.last_at: datetime | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        if not get_settings().goodcang_ready:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="logistics-tickets", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _due(self) -> bool:
        from ..retention import last_due, parse_clock

        s = get_settings()
        with session_scope() as db:
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == "LOGISTICS_TICKET_SYNC"))
        if last is None:
            return True
        if s.logistics_ticket_sync_interval_minutes > 0:  # 每隔 N 分钟一次：谷仓回复了尽快通知买家
            return utcnow() - last >= timedelta(minutes=s.logistics_ticket_sync_interval_minutes)
        due = last_due(datetime.now(), parse_clock(s.logistics_ticket_sync_time))
        return last < due.astimezone(timezone.utc).replace(tzinfo=None)

    def run_now(self) -> dict:
        with self._lock:
            try:
                with session_scope() as db:
                    self.last = sync(db)
            except GoodcangError as e:
                self.last = {"error": str(e)}
            self.last_at = utcnow()
            return self.last

    def _loop(self) -> None:
        self._stop.wait(60)
        while not self._stop.is_set():
            try:
                if self._due():
                    self.run_now()
            except Exception:  # noqa: BLE001
                log.exception("查件单同步出错")
            self._wake.wait(300)
            self._wake.clear()


worker = LogisticsTicketWorker()
