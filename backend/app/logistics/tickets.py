"""谷仓查件单同步（只读）：每天拉最近 LOGISTICS_TICKET_SYNC_DAYS 天的查件单（含客服在谷仓后台手动建的），
按订单号定位店铺和买家会话；谷仓新回复的，把附件（如 POD）存进售后附件，并起草给买家的回复建议，
需要人工的（妥投未收到、退回、可能补发等）把会话标成待售后。回复建议不自动发送，由客服在会话里使用。
"""
from __future__ import annotations

import logging
import re
import threading
import time
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
from .assess import CN
from .goodcang import GoodcangClient, GoodcangError

log = logging.getLogger(__name__)
ACTOR = "logistics_tickets"
# 这些查件类型的结果多半要人工决定（退款、补发、改派等）
HUMAN_TYPES = {"妥投未收到", "退回原因", "拦截", "改派", "查妥投地址"}
EBAY_ORDER = re.compile(r"\d{2}-\d{5}-\d{5}")
HUMAN_WORDS = re.compile(r"丢|遗失|退回|退件|破损|损坏|赔|补发|重发|无法派送|地址|拒收|销毁|扣")


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
    t.reply = (row.get("customer_service_reply") or "").strip() or None
    t.reject_reason = (row.get("reject_reason") or "").strip() or None
    old = {a.get("url"): a for a in (t.attachments or [])}
    t.attachments = [{**old.get(a.get("url"), {}), "name": a.get("name"), "url": a.get("url")}
                     for a in row.get("replay_attachment_file_list") or [] if a.get("url")] or None
    t.added_at, t.replied_at = row.get("add_time") or None, row.get("reply_time") or row.get("last_handle_time") or None
    t.synced_at = now
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
    rec = db.scalar(select(TrackingNumber).where(TrackingNumber.number == (t.tracking_no or "")))
    tr = rec.track or {} if rec else {}
    tracking = {"carrier": tr.get("carrier"), "tracking_number": t.tracking_no, "status": tr.get("status_label"),
                "latest_event": tr.get("latest_event")} if rec else {"tracking_number": t.tracking_no}
    return conv, str(tracking)


def _suggest(db: Session, t: LogisticsTicket, provider) -> None:
    conv_text, tracking = _context(db, t)
    files = [a.get("name") for a in t.attachments or []]
    ticket = (f"查件类型：{t.type_name}\n谷仓状态：{t.io_status_text}\n物流状态：{t.trail_status_text}\n"
              f"服务商回复：{t.reply or '（无文字）'}\n回复附件：{'、'.join(files) or '无'}")
    msgs = [SystemMessage(content=load_prompt("logistics_ticket_reply")),
            HumanMessage(content=f"<conversation>\n{conv_text}\n</conversation>\n<ticket>\n{ticket}\n</ticket>\n"
                                 f"<tracking>\n{tracking}\n</tracking>")]
    provider_name, model = usage_log.chat_model(get_settings())
    started = time.perf_counter()
    r = provider.structured(msgs, TicketReply)
    usage_log.record(db, "ticket_reply", provider=provider_name, model=model, usage=r.usage, ok=r.parsed is not None,
                     error=r.error, duration_ms=int((time.perf_counter() - started) * 1000), store_id=t.store_id,
                     conversation_id=t.conversation_id)
    rule_human = t.type_name in HUMAN_TYPES or bool(HUMAN_WORDS.search(t.reply or ""))
    if r.parsed:
        t.suggestion, t.suggestion_zh = r.parsed.reply_text.strip(), r.parsed.reply_zh.strip() or None
        t.needs_human = rule_human or r.parsed.needs_human
    else:
        t.needs_human = True
    t.suggested_at = utcnow()


def _mark(db: Session, t: LogisticsTicket, client: GoodcangClient | None = None) -> None:
    conv = db.get(Conversation, t.conversation_id) if t.conversation_id else None
    if not conv or conv.aftersales_status in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
        return
    now = utcnow()
    note = f"谷仓查件单 {t.io_code}（{t.type_name}）已回复：{(t.reply or '见附件')[:300]}"
    if client is not None and t.reference_no and HUMAN_WORDS.search(t.reply or ""):  # 可能要补发：附上补发库存
        from .followup import reship_stock

        try:
            note += "；" + reship_stock(client, t.reference_no)["summary"]
        except Exception as e:  # noqa: BLE001  查不到库存也要照常标记
            note += f"；补发库存查询失败：{e if isinstance(e, GoodcangError) else type(e).__name__}"
    conv.aftersales_status, conv.aftersales_type = Aftersales.PENDING, "物流异常/未收到"
    conv.aftersales_note = note[:1000]
    conv.aftersales_marked_by, conv.aftersales_marked_at, conv.aftersales_handled_by = "AI（查件结果）", now, None
    conv.aftersales_updated_at = now
    db.add(AuditEvent(actor=ACTOR, action="AFTERSALES_AUTO_MARKED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"io_code": t.io_code, "type": t.type_name}))


def sync(db: Session, *, client: GoodcangClient | None = None, provider=None, locate=None,
         http: httpx.Client | None = None, suggest_days: int = 3) -> dict:
    s = get_settings()
    client = client or GoodcangClient(s)
    end = _cn_now()
    start = end - timedelta(days=min(186, max(1, s.logistics_ticket_sync_days)))
    fmt = "%Y-%m-%d %H:%M:%S"
    now = utcnow()
    stats = {"tickets": 0, "new": 0, "located": 0, "suggested": 0, "needs_human": 0, "errors": []}
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
            if (t.conversation_id and not t.handled_at and not t.suggested_at
                    and _replied_recently(t, suggest_days)):
                _save_attachments(db, t, http)
                if provider is not None:
                    _suggest(db, t, provider)
                    stats["suggested"] += 1
                else:
                    t.needs_human = True
                if t.needs_human:
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
    """每天 LOGISTICS_TICKET_SYNC_TIME 后同步一次查件单（没开机的启动后补跑）；谷仓没配置时不启动。"""

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

        due = last_due(datetime.now(), parse_clock(get_settings().logistics_ticket_sync_time))
        due_utc = due.astimezone(timezone.utc).replace(tzinfo=None)
        with session_scope() as db:
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == "LOGISTICS_TICKET_SYNC"))
        return last is None or last < due_utc

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
            self._wake.wait(900)
            self._wake.clear()


worker = LogisticsTicketWorker()
