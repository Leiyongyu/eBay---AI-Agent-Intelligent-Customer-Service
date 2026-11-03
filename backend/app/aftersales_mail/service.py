"""售后邮件处理：认订单号 → 定店铺、订单、会话 → 保存附件和 WeTransfer 文件 → 记录；人工认领；每日清理。

订单号能唯一确定店铺：用各店铺的只读订单接口并行查（只有订单所属店铺的授权查得到）。
对不上订单的文件放进“_待认领”，只有管理员和负责全部店铺的账号能看，人工填订单号后自动挪到店铺文件夹并改名。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import (
    AftersalesFile,
    AftersalesMail,
    AsFileStatus,
    AuditEvent,
    Conversation,
    ConversationEvent,
    InvoiceRequest,
    Store,
)
from . import storage, wetransfer
from .parse import ParsedMail, parse

log = logging.getLogger(__name__)
SYSTEM_TYPE = "FROM_EBAY"
ACTOR = "aftersales_mail"


class ClaimError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message


@dataclass
class Located:
    order_id: str | None = None
    store_id: str | None = None
    buyer: str | None = None
    conversation_id: int | None = None
    match: str | None = None


def mask(v: str | None) -> str | None:
    if not v or "@" not in v:
        return v
    name, domain = v.split("@", 1)
    return f"{name[:1]}***@{domain}"


# ------------------------------------------------------------------ 定位订单、店铺、会话
def lookup_order_api(order_id: str) -> tuple[str, str | None] | None:
    """到各店铺用只读订单接口查订单号（8 个店铺并行）；只有订单所属店铺的授权查得到。返回 (店铺, 买家账号)。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ..ebay.stores import is_ebay_store
    from ..sources import SourceNotAvailable, get_ebay_source

    with session_scope() as db:
        stores = list(db.scalars(select(Store.id).where(Store.ebay_user_id.is_not(None))))
    src = get_ebay_source()

    def one(sid: str):
        if not is_ebay_store(sid):
            return None
        try:
            o = src._api(sid).get(f"/sell/fulfillment/v1/order/{order_id}")
        except SourceNotAvailable:
            return None
        return (sid, (o.get("buyer") or {}).get("username")) if o else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, sid) for sid in stores]
        for f in as_completed(futures):
            if f.result():
                for other in futures:
                    other.cancel()
                return f.result()
    return None


def _conversation(db: Session, store_id: str, buyer: str | None) -> int | None:
    if not buyer:
        return None
    return db.scalar(select(Conversation.id).where(
        Conversation.store_id == store_id, func.lower(Conversation.buyer_key) == buyer.lower(),
        Conversation.conversation_type != SYSTEM_TYPE).order_by(Conversation.last_message_at.desc().nulls_last()).limit(1))


def locate_order(db: Session, order_id: str) -> Located | None:
    found = lookup_order_api(order_id)
    if not found:
        return None
    loc = Located(order_id, found[0], found[1], match="order_api")
    loc.conversation_id = _conversation(db, loc.store_id, loc.buyer)
    return loc


def locate(db: Session, mail: ParsedMail) -> Located:
    for order_id in mail.order_ids[:5]:
        loc = locate_order(db, order_id)
        if loc:
            return loc
    if mail.sender:  # 买家在发票申请里留过的邮箱
        req = db.scalar(select(InvoiceRequest).where(func.lower(InvoiceRequest.email) == mail.sender.lower())
                        .order_by(InvoiceRequest.updated_at.desc()).limit(1))
        if req:
            return Located(req.order_id, req.store_id, None, req.conversation_id, "invoice_email")
    return Located(mail.order_ids[0] if mail.order_ids else None)


# ------------------------------------------------------------------ 处理一封邮件
def _folder(db: Session, store_id: str | None) -> str:
    acct = db.scalar(select(Store.ebay_user_id).where(Store.id == store_id)) if store_id else None
    return storage.store_dir(store_id, acct)


def process(db: Session, uid: str, raw: bytes, http: httpx.Client | None = None) -> AftersalesMail:
    """处理一封邮件（幂等：同一 Message-ID 只处理一次）。调用方提交后再把邮件移到“已处理”。"""
    s = get_settings()
    pm = parse(raw)
    message_id = (pm.message_id or f"uid:{s.aftersales_mail_folder}:{uid}")[:300]
    existing = db.scalar(select(AftersalesMail).where(AftersalesMail.message_id == message_id))
    if existing is not None:
        return existing
    loc = locate(db, pm)
    mail = AftersalesMail(message_id=message_id, sender=(pm.sender or "")[:200] or None, subject=pm.subject[:500],
                          received_at=pm.date, order_id=loc.order_id,
                          store_id=loc.store_id, conversation_id=loc.conversation_id, match=loc.match,
                          status="PROCESSED" if loc.store_id else "UNMATCHED", body_excerpt=pm.text.strip()[:2000] or None)
    db.add(mail)
    db.flush()
    folder = _folder(db, loc.store_id)
    now_local = datetime.now()
    seq = 0

    def record(source, name, ctype, status, reason=None, saved=None, scan=None, link=None):
        db.add(AftersalesFile(mail_id=mail.id, store_id=loc.store_id, order_id=loc.order_id,
                              conversation_id=loc.conversation_id, source=source, original_name=(name or "")[:300] or None,
                              content_type=(saved.content_type if saved else ctype)[:100] if (saved or ctype) else None,
                              size_bytes=saved.size if saved else None, sha256=saved.sha256 if saved else None,
                              rel_path=saved.rel_path if saved else None, link_url=(link or "")[:1000] or None,
                              status=status, status_reason=reason, scan_result=scan))

    def store_chunks(source, name, ctype, chunks, link=None):
        nonlocal seq
        ext = storage.ext_of(name, ctype)
        if not storage.allowed(ext):
            record(source, name, ctype, AsFileStatus.BLOCKED_TYPE, f"文件类型 {ext or '未知'} 不允许", link=link)
            return
        seq += 1
        try:
            saved = storage.save_stream(chunks, folder, storage.file_name(loc.order_id, now_local, seq, ext), ctype)
        except storage.FileTooLarge:
            record(source, name, ctype, AsFileStatus.TOO_LARGE,
                   f"超过单文件上限 {s.aftersales_file_max_mb} MB，已丢弃，请人工处理", link=link)
            return
        result, note = storage.scan(saved.rel_path)
        if result == "THREAT":
            record(source, name, ctype, AsFileStatus.INFECTED, note, scan=result, link=link)
            return
        record(source, name, ctype, AsFileStatus.STORED, note, saved=saved, scan=result, link=link)

    for att in pm.attachments:
        store_chunks("ATTACHMENT", att.filename, att.content_type, [att.data])

    if pm.wetransfer_links:
        client = http or httpx.Client(timeout=httpx.Timeout(60, read=300), headers={"User-Agent": wetransfer.UA})
        try:
            for link in pm.wetransfer_links:
                try:
                    name, ctype, chunks, resp = wetransfer.open_download(link, client)
                except (wetransfer.WeTransferError, httpx.HTTPError, ValueError) as e:
                    msg = str(e) if isinstance(e, wetransfer.WeTransferError) else f"下载失败：{type(e).__name__}"
                    record("WETRANSFER", None, None, AsFileStatus.DOWNLOAD_FAILED, msg + "；请人工下载", link=link)
                    continue
                try:
                    store_chunks("WETRANSFER", name, ctype, chunks, link=link)
                except httpx.HTTPError as e:
                    record("WETRANSFER", name, ctype, AsFileStatus.DOWNLOAD_FAILED,
                           f"下载中断：{type(e).__name__}；请人工下载", link=link)
                finally:
                    resp.close()
        finally:
            if http is None:
                client.close()
    for link in pm.other_links:
        record("LINK", None, None, AsFileStatus.DOWNLOAD_FAILED, "这类分享链接系统不自动下载，请人工下载", link=link)

    db.flush()
    mail.files = db.scalar(select(func.count()).select_from(AftersalesFile).where(
        AftersalesFile.mail_id == mail.id, AftersalesFile.status == AsFileStatus.STORED)) or 0
    if loc.conversation_id:
        conv = db.get(Conversation, loc.conversation_id)
        db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type="AFTERSALES_FILES_RECEIVED",
                                 source=ACTOR, detail=f"售后邮箱收到邮件（订单 {loc.order_id or '未知'}），"
                                                      f"保存 {mail.files} 个文件"))
        from ..read_state import mark_unread

        mark_unread(db, conv, ACTOR)
    db.add(AuditEvent(actor=ACTOR, action="AFTERSALES_MAIL_PROCESSED", store_id=loc.store_id,
                      conversation_id=loc.conversation_id,
                      detail={"mail_id": mail.id, "from": mask(pm.sender), "order_id": loc.order_id, "match": loc.match,
                              "files": mail.files, "status": mail.status}))
    db.flush()
    return mail


# ------------------------------------------------------------------ 人工认领 / 改关联
def claim(db: Session, mail: AftersalesMail, order_id: str, actor: str, can_see) -> AftersalesMail:
    """把邮件（连同文件）关联到订单：挪到订单所属店铺的文件夹并按规则改名。can_see(store_id) 检查店铺权限。"""
    order_id = (order_id or "").strip()
    loc = locate_order(db, order_id) if order_id else None
    if loc is None:
        raise ClaimError(404, f"查不到订单 {order_id}（本地和各店铺都没有）")
    if not can_see(loc.store_id) or (mail.store_id and not can_see(mail.store_id)):
        raise ClaimError(403, "这个订单不在你负责的店铺")
    folder = _folder(db, loc.store_id)
    now_local = datetime.now()
    files = db.scalars(select(AftersalesFile).where(AftersalesFile.mail_id == mail.id).order_by(AftersalesFile.id)).all()
    for i, f in enumerate(files, 1):
        if f.status == AsFileStatus.STORED and f.rel_path:
            ext = storage.ext_of(f.original_name or f.rel_path, f.content_type)
            f.rel_path = storage.move(f.rel_path, folder, storage.file_name(order_id, now_local, i, ext))
        f.store_id, f.order_id, f.conversation_id = loc.store_id, order_id, loc.conversation_id
        f.claimed_by, f.claimed_at = actor[:64], utcnow()
    old = mail.store_id
    mail.store_id, mail.order_id, mail.conversation_id = loc.store_id, order_id, loc.conversation_id
    mail.match, mail.status = "manual", "PROCESSED"
    storage.remove_empty_dirs()
    db.add(AuditEvent(actor=actor[:64], action="AFTERSALES_FILES_CLAIMED", store_id=loc.store_id,
                      conversation_id=loc.conversation_id,
                      detail={"mail_id": mail.id, "order_id": order_id, "from_store": old, "files": len(files)}))
    db.flush()
    return mail


# ------------------------------------------------------------------ 每日清理
def cleanup(db: Session, now: datetime | None = None, mailbox=None) -> dict:
    """删掉满保留期的文件（记录留着、标“已清理”）、清空过期邮件的发件人和正文、删“已处理”里的过期邮件、删空文件夹。"""
    s = get_settings()
    now = now or utcnow()
    cutoff = now - timedelta(days=s.aftersales_retention_days)
    root = s.aftersales_root
    n_files = 0
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.status == AsFileStatus.STORED,
                                                     AftersalesFile.received_at < cutoff)):
        if f.rel_path:
            (root / f.rel_path).unlink(missing_ok=True)
        f.status, f.purged_at = AsFileStatus.PURGED, now
        f.status_reason = f"满 {s.aftersales_retention_days} 天自动清理"
        n_files += 1
    n_mails = 0
    for m in db.scalars(select(AftersalesMail).where(AftersalesMail.processed_at < cutoff,
                                                     AftersalesMail.body_excerpt.is_not(None) | AftersalesMail.sender.is_not(None))):
        m.sender = m.body_excerpt = None
        n_mails += 1
    purged_mails, mail_error = 0, None
    if mailbox is not None:
        try:
            purged_mails = mailbox.purge_done(cutoff.date())
        except Exception as e:  # noqa: BLE001 — 邮箱清理失败不影响文件清理
            mail_error = str(e)[:300]
    from ..upload_page.service import cleanup as upload_cleanup

    uploads = upload_cleanup(db, now)  # 上传页：R2 里没传完/没拉回的文件、过期的上传记录
    dirs = storage.remove_empty_dirs()
    result = {"files": n_files, "mail_records": n_mails, "mailbox_deleted": purged_mails, "empty_dirs": dirs,
              "mailbox_error": mail_error, "cutoff": cutoff.isoformat(timespec="seconds") + "Z", **uploads}
    db.add(AuditEvent(actor=ACTOR, action="AFTERSALES_CLEANUP", detail=result))
    db.flush()
    return result


# ------------------------------------------------------------------ 后台任务
def run_once() -> dict:
    """收一轮邮件。每封单独提交，提交后才移到“已处理”；处理出错的留在收件箱，下轮再试。"""
    from .mailbox import ImapMailbox, MailboxError

    s = get_settings()
    if s.aftersales_mail_not_ready_reasons():
        return {"skipped": s.aftersales_mail_not_ready_reasons()}
    done = failed = 0
    try:
        with ImapMailbox() as box:
            for uid, raw in box.fetch_all():
                try:
                    with session_scope() as db:
                        process(db, uid, raw)
                    box.move_done(uid)
                    done += 1
                except MailboxError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("处理售后邮件失败（uid=%s），留在收件箱下轮再试", uid)
                    failed += 1
    except MailboxError as e:
        log.warning("售后邮箱：%s", e)
        return {"error": str(e), "processed": done, "failed": failed}
    return {"processed": done, "failed": failed}


class AftersalesMailWorker:
    """每 AFTERSALES_MAIL_POLL_SECONDS 秒收一次邮件；每天 AFTERSALES_CLEANUP_TIME 后清理一次（没开机的启动后补跑）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll: dict | None = None
        self.last_poll_at: datetime | None = None
        self.last_cleanup: dict | None = None
        self.last_pull: dict | None = None
        self._wake = threading.Event()

    def start(self) -> bool:
        s = get_settings()
        if s.aftersales_mail_not_ready_reasons() and s.upload_not_ready_reasons() and not s.aftersales_root.exists():
            return False  # 邮箱、上传页都没配，也没有存过文件：不用起
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="aftersales-mail", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _cleanup_due(self) -> bool:
        from ..retention import last_due, parse_clock

        due_local = last_due(datetime.now(), parse_clock(get_settings().aftersales_cleanup_time))
        due_utc = due_local.astimezone(timezone.utc).replace(tzinfo=None)
        with session_scope() as db:
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == "AFTERSALES_CLEANUP"))
        return last is None or last < due_utc

    def run_cleanup(self) -> dict:
        from .mailbox import ImapMailbox, MailboxError

        box = None
        try:
            if not get_settings().aftersales_mail_not_ready_reasons():
                box = ImapMailbox().__enter__()
        except MailboxError as e:
            log.warning("售后邮箱清理时连不上邮箱：%s", e)
        try:
            with session_scope() as db:
                self.last_cleanup = cleanup(db, mailbox=box)
        finally:
            if box is not None:
                box.__exit__(None, None, None)
        return self.last_cleanup

    def _loop(self) -> None:
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.last_poll = run_once()
                self.last_poll_at = utcnow()
                from ..upload_page.service import pull_once

                self.last_pull = pull_once()  # 上传页：把买家已提交的文件从 R2 拉回
                if self._cleanup_due():
                    self.run_cleanup()
            except Exception:  # noqa: BLE001
                log.exception("售后邮箱后台任务出错")
            self._wake.wait(max(60, get_settings().aftersales_mail_poll_seconds))
            self._wake.clear()


worker = AftersalesMailWorker()
