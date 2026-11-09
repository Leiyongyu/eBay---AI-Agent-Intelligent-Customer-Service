"""售后邮件处理：认订单号 → 定店铺、订单、会话 → 保存附件和 WeTransfer 文件 → 记录；人工认领；每日清理。

订单号能唯一确定店铺：用各店铺的只读订单接口并行查（只有订单所属店铺的授权查得到）。
对不上订单的文件放进“_待认领”，只有管理员和负责全部店铺的账号能看，人工填订单号后自动挪到店铺文件夹并改名。
"""
from __future__ import annotations

import logging
import re
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
    MailboxCursor,
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


def order_conversation(db: Session, store_id: str, buyer: str | None, order_id: str | None,
                       item_ids: list[str] | None = None) -> int | None:
    """买家可能有多个会话：优先找同一刊登（订单商品）的会话，其次按订单建的通知会话，最后才是买家最近的会话。"""
    if not buyer:
        return None
    base = select(Conversation).where(Conversation.store_id == store_id, func.lower(Conversation.buyer_key) == buyer.lower(),
                                      Conversation.conversation_type != SYSTEM_TYPE)
    items = [str(i) for i in item_ids or [] if i]
    if items:
        cid = db.scalar(select(Conversation.id).where(Conversation.id.in_(base.with_only_columns(Conversation.id)),
                                                      Conversation.item_id.in_(items))
                        .order_by(Conversation.last_message_at.desc().nulls_last()).limit(1))
        if cid:
            return cid
    if order_id:
        cid = db.scalar(select(Conversation.id).where(Conversation.store_id == store_id,
                                                      Conversation.ebay_conversation_id == f"ORDER-{order_id}"))
        if cid:
            return cid
    return _conversation(db, store_id, buyer)


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
    by_name = locate_buyer_name(db, f"{mail.subject}\n{(mail.text or '')[:3000]}")
    if by_name:
        return by_name
    return Located(mail.order_ids[0] if mail.order_ids else None)


_NAME_TOKEN = re.compile(r"(?<![\w@.])[A-Za-z0-9][A-Za-z0-9._\-*]{2,62}[A-Za-z0-9*](?![\w@])")


def locate_buyer_name(db: Session, text: str) -> Located | None:
    """邮件主题 / 正文里写了买家的 eBay 用户名（WeTransfer 通知邮件里买家填的标题、留言）：对到这个买家最近的会话。
    同一个用户名在多个店铺都有会话时拿不准，交人工认领。"""
    tokens = {t.lower() for t in _NAME_TOKEN.findall(text or "") if not t.isdigit()}
    if not tokens:
        return None
    rows = db.execute(select(Conversation.id, Conversation.store_id, Conversation.buyer_key)
                      .where(func.lower(Conversation.buyer_key).in_(tokens), Conversation.conversation_type != "FROM_EBAY")
                      .order_by(Conversation.last_message_at.desc().nulls_last())).all()
    if not rows or len({(r.store_id, r.buyer_key.lower()) for r in rows}) != 1:
        return None
    conv_id, store_id, buyer = rows[0]
    from ..models import AftersalesCase

    order_id = db.scalar(select(AftersalesCase.order_id).where(AftersalesCase.conversation_id == conv_id,
                                                               AftersalesCase.order_id.is_not(None))
                         .order_by(AftersalesCase.id.desc()).limit(1))
    return Located(order_id, store_id, buyer, conv_id, "ebay_name")


# ------------------------------------------------------------------ 是不是售后邮件
# 这些发件方的邮件和买家售后无关：谷仓的查件进度通知（查件结果走谷仓接口）
IGNORE_SENDER_DOMAINS = ("goodcang.com", "goodcang.net")
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv", ".3gp", ".wmv", ".mpg", ".mpeg"}


def _domain(addr: str | None) -> str:
    return (addr or "").rsplit("@", 1)[-1].lower() if addr and "@" in addr else ""


def _under(domain: str, roots) -> bool:
    return any(domain == r or domain.endswith("." + r) for r in roots)


def relevance(db: Session, pm: ParsedMail) -> str | None:
    """这封邮件和买家售后有没有关系；有关返回依据，无关返回 None（不记录、不移动，留在收件箱给同事自己看）。
    售后邮箱可能就是同事的工作邮箱，系统告警、内部往来、谷仓通知、广告都不收。有关的：
    写了 eBay 订单号；带 WeTransfer 下载链接；带视频附件；发件人是买家在发票申请里留的邮箱；
    主题 / 正文写了某个买家的 eBay 用户名（只对得上一个会话时）。本公司邮箱发的只认订单号、WeTransfer、视频（同事转发）。"""
    sender_domain = _domain(pm.sender)
    if _under(sender_domain, IGNORE_SENDER_DOMAINS):
        return None
    if pm.order_ids:
        return "order_id"
    if pm.wetransfer_links:
        return "wetransfer"
    if any(storage.ext_of(a.filename, a.content_type) in VIDEO_EXTS for a in pm.attachments):
        return "video"
    own = _domain(get_settings().aftersales_mail_user)
    if own and _under(sender_domain, (own,)):
        return None
    if pm.sender and db.scalar(select(InvoiceRequest.id).where(func.lower(InvoiceRequest.email) == pm.sender.lower()).limit(1)):
        return "invoice_email"
    if locate_buyer_name(db, pm.subject + "\n" + (pm.text or "")[:3000]):
        return "ebay_name"
    return None


# ------------------------------------------------------------------ 处理一封邮件
def _folder(db: Session, store_id: str | None) -> str:
    acct = db.scalar(select(Store.ebay_user_id).where(Store.id == store_id)) if store_id else None
    return storage.store_dir(store_id, acct)


def process(db: Session, uid: str, raw: bytes, http: httpx.Client | None = None,
            pm: ParsedMail | None = None) -> AftersalesMail:
    """处理一封邮件（幂等：同一 Message-ID 只处理一次）。调用方提交后再把邮件移到“已处理”。
    只该处理和售后有关的邮件（先用 relevance 判断）。"""
    s = get_settings()
    pm = pm or parse(raw)
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
    # 谷仓查件单回复的附件（POD 签收证明等）是纠纷证据，单独按 LOGISTICS_POD_RETENTION_DAYS 保留
    pod_cutoff = now - timedelta(days=max(s.logistics_pod_retention_days, s.aftersales_retention_days))
    root = s.aftersales_root
    n_files = 0
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.status == AsFileStatus.STORED,
                                                     AftersalesFile.received_at < cutoff)):
        pod = f.source == "GOODCANG_TICKET"
        if pod and f.received_at >= pod_cutoff:
            continue
        if f.rel_path:
            (root / f.rel_path).unlink(missing_ok=True)
        f.status, f.purged_at = AsFileStatus.PURGED, now
        f.status_reason = f"满 {s.logistics_pod_retention_days if pod else s.aftersales_retention_days} 天自动清理"
        n_files += 1
    n_mails = 0
    for m in db.scalars(select(AftersalesMail).where(AftersalesMail.processed_at < cutoff,
                                                     AftersalesMail.body_excerpt.is_not(None) | AftersalesMail.sender.is_not(None))):
        m.sender = m.body_excerpt = None
        n_mails += 1
    purged_mails, mail_error = 0, None
    if mailbox is not None:
        try:  # 只删本系统处理过的邮件：别人自己放进“已处理”的、以前误移进去的无关邮件都不碰
            ours = set(db.scalars(select(AftersalesMail.message_id).where(~AftersalesMail.message_id.startswith("uid:"))))
            purged_mails = mailbox.purge_done(cutoff.date(), ours)
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
FIRST_RUN_DAYS = 3  # 第一次收信（或 UIDVALIDITY 变了）往回看几天


def run_once() -> dict:
    """收一轮新邮件（UID 大于上次看到的）。和售后无关的不记录、不移动、不改已读，只把进度往后推；
    有关的处理后单独提交，再移到“已处理”。处理出错的留在收件箱，进度停在它前面，下轮再试。"""
    from .mailbox import ImapMailbox, MailboxError

    s = get_settings()
    if s.aftersales_mail_not_ready_reasons():
        return {"skipped": s.aftersales_mail_not_ready_reasons()}
    folder = s.aftersales_mail_folder
    done = failed = skipped = 0
    try:
        with ImapMailbox() as box:
            with session_scope() as db:
                cur = db.get(MailboxCursor, folder)
                after, validity = (cur.last_uid, cur.uidvalidity) if cur else (None, None)
                retry_uid, retries = (cur.retry_uid, cur.retries or 0) if cur else (None, 0)
            since = (datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)).date()
            old_validity = validity
            validity, mails = box.fetch_new(after, validity, since)
            last = (after or 0) if after is not None and validity == old_validity else 0
            for uid, raw in mails:
                try:
                    with session_scope() as db:
                        pm = parse(raw)
                        ours = relevance(db, pm) is not None
                        if ours:
                            process(db, uid, raw, pm=pm)
                    if ours:  # 提交后才移走
                        box.move_done(uid)
                        done += 1
                    else:
                        skipped += 1
                except MailboxError:
                    raise
                except Exception:  # noqa: BLE001
                    failed += 1
                    retries = retries + 1 if retry_uid == int(uid) else 1
                    retry_uid = int(uid)
                    if retries < 3:
                        log.exception("处理售后邮件失败（uid=%s，第 %s 次），留在收件箱下轮再试", uid, retries)
                        break  # 进度停在这封前面，下轮从它重试
                    log.exception("售后邮件 uid=%s 连续 %s 次处理失败，跳过（留在收件箱人工看）", uid, retries)
                last = max(last, int(uid))
                if retry_uid == int(uid) and retries >= 3:
                    retry_uid, retries = None, 0
            with session_scope() as db:
                cur = db.get(MailboxCursor, folder) or MailboxCursor(folder=folder, last_uid=0, skipped=0, retries=0)
                db.add(cur)
                cur.uidvalidity, cur.last_uid = validity, last
                cur.retry_uid, cur.retries = (retry_uid, retries) if failed and retry_uid else (None, 0)
                cur.skipped, cur.updated_at = (cur.skipped or 0) + skipped, utcnow()
    except MailboxError as e:
        log.warning("售后邮箱：%s", e)
        return {"error": str(e), "processed": done, "failed": failed, "skipped": skipped}
    return {"processed": done, "failed": failed, "skipped": skipped}


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
