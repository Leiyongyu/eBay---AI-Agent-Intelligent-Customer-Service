"""买家在 eBay 消息里发来的视频链接：自动下载存进售后附件，对上订单，飞书工单再把视频发到话题里。

eBay 消息里发不了视频，售后问题请买家把视频传到 WeTransfer，再按模板在 eBay 消息里把订单号和链接一起发来：
“我的订单号是：04-15163-40358，我的视频链接为：https://we.tl/t-xxxx”。
- WeTransfer 链接（we.tl / wetransfer.com）：自动下载（同售后邮箱那一套存储、扫描、大小上限），按“店铺 / 订单号_时间_序号”存放；
- 其他网盘链接（Google Drive、Dropbox、OneDrive…）：登记下来提醒人工下载；
- 订单号：消息里写了就用它，没写用会话里售后工单的订单号；文件同时关联会话，飞书工单按会话或订单号取附件。
同一个链接只处理一次（下载失败的也不反复重试，工作台可人工下载）。只看最近 MESSAGE_LINK_DAYS 天的买家消息。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import AftersalesCase, AftersalesFile, AsFileStatus, AuditEvent, Conversation, ConversationEvent, Message
from . import storage, wetransfer
from .parse import OTHER_SHARE_HOSTS, WETRANSFER_HOSTS, _host_in, wetransfer_downloads
from .service import _folder

log = logging.getLogger(__name__)
ACTOR = "售后附件（买家消息里的链接）"
URL_RE = re.compile(r"(?:https?://|\bwe\.tl/)[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+", re.I)  # 网址只有 ASCII：紧跟的中文不算进去
ORDER_RE = re.compile(r"\b\d{2}-\d{5}-\d{5}\b")
MESSAGE_LINK_DAYS = 7


def links_in(text: str) -> tuple[list[str], list[str]]:
    """（WeTransfer 链接，其他网盘链接），去掉句末标点；没写 https:// 的 we.tl 短链补上。"""
    found = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?，。；：！？")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in found:
            found.append(u)
    return (wetransfer_downloads(found),  # 只要真正的文件链接，买家只贴了 WeTransfer 首页的不算
            [u for u in found if _host_in(u, OTHER_SHARE_HOSTS) and not _host_in(u, WETRANSFER_HOSTS)])


def _order_for(db: Session, conv: Conversation, text: str) -> str | None:
    m = ORDER_RE.search(text or "")
    if m:
        return m.group(0)
    case_order = db.scalar(select(AftersalesCase.order_id).where(AftersalesCase.conversation_id == conv.id,
                                                                 AftersalesCase.order_id.is_not(None))
                           .order_by(AftersalesCase.id.desc()).limit(1))
    return case_order or _only_order(db, conv)


def _only_order(db: Session, conv: Conversation) -> str | None:
    """买家在这个店铺最近 90 天只有一笔订单：就是它（多笔或没有就不猜，回复里请买家给订单号）。
    读 ebay_orders（售后同步每小时更新）；原来读的 sales_orders 是已删除的数据助手留下的旧表，早已不更新。"""
    from sqlalchemy import func

    from ..models import EbayOrder

    if not conv.buyer_key:
        return None
    rows = db.scalars(select(EbayOrder.order_id).where(
        EbayOrder.store_id == conv.store_id, func.lower(EbayOrder.buyer) == conv.buyer_key.lower(),
        EbayOrder.cancelled.is_(False), EbayOrder.created_at >= utcnow() - timedelta(days=90)).limit(2)).all()
    return rows[0] if len(rows) == 1 else None


def backfill_orders(db: Session) -> int:
    """买家先发了链接、后来才补订单号：把已下载的文件补上订单号。"""
    n = 0
    files = db.scalars(select(AftersalesFile).where(AftersalesFile.order_id.is_(None), AftersalesFile.conversation_id.is_not(None),
                                                    AftersalesFile.source.in_(("WETRANSFER", "LINK")),
                                                    AftersalesFile.received_at >= utcnow() - timedelta(days=MESSAGE_LINK_DAYS))).all()
    for f in files:
        msgs = db.scalars(select(Message.body).where(Message.conversation_id == f.conversation_id,
                                                     Message.sender_role == "BUYER")
                          .order_by(Message.conversation_seq.desc()).limit(20)).all()
        body = next((t for t in msgs if ORDER_RE.search(t or "")), None)
        m = ORDER_RE.search(body or "")
        if m:
            f.order_id, n = m.group(0), n + 1
    return n


def _save(db: Session, conv: Conversation, msg: Message, order_id: str | None, link: str, http: httpx.Client) -> AftersalesFile:
    s = get_settings()

    def record(status, reason=None, name=None, ctype=None, saved=None, scan=None):
        f = AftersalesFile(mail_id=None, store_id=conv.store_id, order_id=order_id, conversation_id=conv.id,
                           source="WETRANSFER" if _host_in(link, WETRANSFER_HOSTS) else "LINK",
                           original_name=(name or "")[:300] or None,
                           content_type=((saved.content_type if saved else ctype) or "")[:100] or None,
                           size_bytes=saved.size if saved else None, sha256=saved.sha256 if saved else None,
                           rel_path=saved.rel_path if saved else None, link_url=link[:1000], status=status,
                           status_reason=reason or f"买家在 eBay 消息里发来的链接（消息 {msg.ebay_message_id}）", scan_result=scan)
        db.add(f)
        db.flush()
        return f

    if not _host_in(link, WETRANSFER_HOSTS):
        return record(AsFileStatus.DOWNLOAD_FAILED, "这类网盘链接系统不自动下载，请人工下载")
    try:
        name, ctype, chunks, resp = wetransfer.open_download(link, http)
    except (wetransfer.WeTransferError, httpx.HTTPError, ValueError) as e:
        msg_ = str(e) if isinstance(e, wetransfer.WeTransferError) else f"下载失败：{type(e).__name__}"
        return record(AsFileStatus.DOWNLOAD_FAILED, msg_ + "（链接可能已过期）；请人工下载")
    try:
        ext = storage.ext_of(name, ctype)
        if not storage.allowed(ext):
            return record(AsFileStatus.BLOCKED_TYPE, f"文件类型 {ext or '未知'} 不允许", name, ctype)
        seq = 1 + len(db.scalars(select(AftersalesFile.id).where(AftersalesFile.conversation_id == conv.id)).all())
        try:
            saved = storage.save_stream(chunks, _folder(db, conv.store_id),
                                        storage.file_name(order_id, datetime.now(), seq, ext), ctype)
        except storage.FileTooLarge:
            return record(AsFileStatus.TOO_LARGE, f"超过单文件上限 {s.aftersales_file_max_mb} MB，已丢弃，请人工处理", name, ctype)
        except httpx.HTTPError as e:
            return record(AsFileStatus.DOWNLOAD_FAILED, f"下载中断：{type(e).__name__}；请人工下载", name, ctype)
        result, note = storage.scan(saved.rel_path)
        if result == "THREAT":
            return record(AsFileStatus.INFECTED, note, name, ctype, scan=result)
        return record(AsFileStatus.STORED, note, name, ctype, saved=saved, scan=result)
    finally:
        resp.close()


def run(db: Session, http: httpx.Client | None = None) -> dict:
    """处理一遍最近买家消息里的链接。返回 {stored, failed}。"""
    stats = {"stored": 0, "failed": 0, "orders_filled": backfill_orders(db)}
    since = utcnow() - timedelta(days=MESSAGE_LINK_DAYS)
    msgs = db.scalars(select(Message).where(Message.sender_role == "BUYER", Message.sent_at >= since,
                                            Message.body.ilike("%http%") | Message.body.ilike("%we.tl%"))
                      .order_by(Message.id)).all()
    todo = []
    for m in msgs:
        wt, other = links_in(m.body or "")
        for link in wt + other:
            if not db.scalar(select(AftersalesFile.id).where(AftersalesFile.link_url == link[:1000]).limit(1)):
                todo.append((m, link))
    if not todo:
        return stats
    client = http or httpx.Client(timeout=httpx.Timeout(60, read=300), headers={"User-Agent": wetransfer.UA})
    try:
        for m, link in todo:
            conv = db.get(Conversation, m.conversation_id)
            if conv is None:
                continue
            order_id = _order_for(db, conv, m.body or "")
            with db.begin_nested():
                f = _save(db, conv, m, order_id, link, client)
            ok = f.status == AsFileStatus.STORED
            stats["stored" if ok else "failed"] += 1
            db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type="AFTERSALES_FILES_RECEIVED",
                                     source=ACTOR, detail=(f"已下载买家发来的视频 / 文件（订单 {order_id or '未知'}）" if ok else
                                                           f"买家发来的链接没能自动下载：{f.status_reason}")[:500]))
            db.add(AuditEvent(actor=ACTOR, action="MESSAGE_LINK_FETCHED" if ok else "MESSAGE_LINK_FAILED",
                              store_id=conv.store_id, conversation_id=conv.id,
                              detail={"file_id": f.id, "order_id": order_id, "status": f.status, "message": m.ebay_message_id}))
            db.commit()
    finally:
        if http is None:
            client.close()
    return stats


class MessageLinkWorker:
    """每分钟看一次最近的买家消息有没有新的视频链接。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not get_settings().message_links_enabled:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="message-links", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._stop.wait(45)
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    stats = run(db)
                if stats["stored"] or stats["failed"]:
                    log.info("买家消息里的视频链接：下载 %s 个，失败 %s 个", stats["stored"], stats["failed"])
            except Exception:  # noqa: BLE001
                log.exception("处理买家消息里的链接出错")
            self._stop.wait(60)


worker = MessageLinkWorker()
