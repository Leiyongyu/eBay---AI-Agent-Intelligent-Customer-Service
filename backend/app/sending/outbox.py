"""发件箱：所有发给买家的消息（人工回复、AI 自动回复、已收到回执、查件结果、查件提醒、发货通知、发票提醒）都先进这里。

- 提交后等 SEND_DELAY_SECONDS（默认 5 分钟）才由 Worker 发出，这段时间里任何人都可以撤回；撤回后回复任务回到待审核、草稿可再改；
- 撤回和发出用“只改 PENDING 的那一行”互斥：撤回成功就一定不会发出；
- 发送时按店铺选渠道（channel.get_channel）：对接了 eBay 的店铺真实调用 sendMessage，其余只记在本地；
- 发出后才把消息写进会话；结果不确定（超时、5xx）的标 UNCERTAIN，不自动重发，需人工到 eBay 核对；
- SEND_DELAY_SECONDS=0 时提交即发（测试用）。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope, utcnow
from ..ingestion.service import _append_message
from ..models import (AuditEvent, Conversation, Draft, JobState, OutboundMessage, ReplyJob, SendAttempt, SendIntent)
from .channel import ChannelNotConfigured, ChannelUncertain, get_channel

log = logging.getLogger(__name__)

SIMULATED_PREFIX = "SIM-OUT-"
STALE_SENDING = timedelta(minutes=10)  # 发送中超过这么久（多半是程序中途退出）：当作结果不确定


class OutboxError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status_code, message


def enqueue(db: Session, conv: Conversation, text: str, *, actor: str, source: str, zh: str | None = None,
            job_id: int | None = None, meta: dict | None = None, prefix: str = SIMULATED_PREFIX) -> OutboundMessage:
    """放进发件箱，延迟到期后发出。延迟为 0 时立即发出。"""
    delay = max(0, get_settings().send_delay_seconds)
    now = utcnow()
    ob = OutboundMessage(store_id=conv.store_id, conversation_id=conv.id, body=text, zh_translation=zh, source=source,
                         actor=actor[:64], job_id=job_id, state="PENDING", send_after=now + timedelta(seconds=delay),
                         created_at=now, meta={**(meta or {}), "prefix": prefix})
    db.add(ob)
    db.flush()
    db.add(AuditEvent(actor=actor[:64], action="OUTBOUND_QUEUED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"outbound_id": ob.id, "source": source, "job_id": job_id, "delay_seconds": delay}))
    if delay == 0:
        ob.state = "SENDING"
        deliver(db, ob)
    db.flush()
    return ob


def recall(db: Session, ob_id: int, actor: str) -> OutboundMessage:
    """撤回还没发出的消息。已经发出或正在发送的不能撤回（eBay 上发出去的消息收不回来）。"""
    ob = db.get(OutboundMessage, ob_id)
    if ob is None:
        raise OutboxError(404, "这条待发消息不存在")
    n = db.execute(update(OutboundMessage).where(OutboundMessage.id == ob_id, OutboundMessage.state == "PENDING")
                   .values(state="RECALLED", recalled_by=actor[:64], recalled_at=utcnow(), updated_at=utcnow())).rowcount
    if n != 1:
        db.refresh(ob)
        raise OutboxError(409, "已经发出或正在发送，不能撤回" if ob.state in ("SENDING", "SENT", "UNCERTAIN")
                          else "这条消息已经撤回或没有发出")
    db.refresh(ob)
    _undo(db, ob, "RECALLED", f"{actor} 撤回")
    db.add(AuditEvent(actor=actor[:64], action="OUTBOUND_RECALLED", store_id=ob.store_id, conversation_id=ob.conversation_id,
                      detail={"outbound_id": ob.id, "source": ob.source, "job_id": ob.job_id}))
    db.flush()
    return ob


def deliver(db: Session, ob: OutboundMessage) -> None:
    """真正发出（调用前已把状态改成 SENDING）。"""
    conv = db.get(Conversation, ob.conversation_id)
    meta = ob.meta or {}
    channel = get_channel(db, conv, meta.get("prefix") or SIMULATED_PREFIX)
    ob.channel = channel.name
    intent = db.scalar(select(SendIntent).where(SendIntent.job_id == ob.job_id)) if ob.job_id else None
    try:
        receipt = channel.send(db, conv, ob.body, media=meta.get("ebay_media"))
    except ChannelNotConfigured as e:
        ob.state, ob.error = "FAILED", str(e)[:1000]
        _attempt(db, intent, "NOT_SENT", "rejected")
        _undo(db, ob, "FAILED", ob.error)
        _audit(db, ob, "OUTBOUND_FAILED")
        return
    except ChannelUncertain as e:
        ob.state, ob.error = "UNCERTAIN", str(e)[:1000]
        _attempt(db, intent, "UNCERTAIN", "timeout")
        if intent is not None:
            intent.state, intent.last_error = "SEND_UNCERTAIN", ob.error
        job = db.get(ReplyJob, ob.job_id) if ob.job_id else None
        if job is not None and job.state == JobState.SENDING:
            job.state = JobState.SEND_UNCERTAIN
        _audit(db, ob, "OUTBOUND_UNCERTAIN")
        return
    msg = _append_message(db, conv, receipt.platform_message_id, "SELLER", "OUT", ob.body, None)
    msg.zh_translation = ob.zh_translation
    if meta.get("media"):
        msg.media = meta["media"]
    if ob.source == "ack":
        msg.auto_kind = "ACK"
    conv.row_version += 1
    now = utcnow()
    ob.state, ob.sent_at, ob.platform_message_id, ob.message_id, ob.error = "SENT", now, receipt.platform_message_id, msg.id, None
    _attempt(db, intent, "SENT", receipt.http_status)
    if intent is not None and intent.state == "QUEUED":
        intent.state, intent.platform_message_id, intent.sent_at = "SENT", receipt.platform_message_id, now
    job = db.get(ReplyJob, ob.job_id) if ob.job_id else None
    if job is not None and job.state == JobState.SENDING:
        job.state = JobState.SENT
        job.lock_version += 1
    _on_sent(db, ob, msg)
    _audit(db, ob, "OUTBOUND_SENT")


def confirm_from_sync(db: Session, ob: OutboundMessage, msg, actor: str = "ebay_sync") -> None:
    """发送时结果不确定（UNCERTAIN，没记下 eBay 消息 ID），eBay 同步拉回了正文一致的店铺消息：其实发出去了。
    补成 SENT、记下 eBay 消息 ID 和会话里那条消息（同步已经写进会话，不再写第二条、不重发）；任务、发送意图、来源状态一并补成已发出。"""
    intent = db.scalar(select(SendIntent).where(SendIntent.job_id == ob.job_id)) if ob.job_id else None
    before = ob.state
    ob.state, ob.sent_at = "SENT", ob.sent_at or msg.sent_at or utcnow()
    ob.platform_message_id, ob.message_id, ob.error = msg.ebay_message_id, msg.id, None
    if ob.zh_translation and not msg.zh_translation:
        msg.zh_translation = ob.zh_translation
    if ob.source == "ack":
        msg.auto_kind = "ACK"
    _attempt(db, intent, "SENT", "confirmed_by_sync")
    if intent is not None and intent.state in ("SEND_UNCERTAIN", "QUEUED", "SENDING"):
        intent.state, intent.platform_message_id, intent.sent_at = "SENT", msg.ebay_message_id, ob.sent_at
        intent.last_error = None
    job = db.get(ReplyJob, ob.job_id) if ob.job_id else None
    if job is not None and job.state in (JobState.SEND_UNCERTAIN, JobState.SENDING):
        job.state = JobState.SENT
        job.lock_version += 1
    _on_sent(db, ob, msg)
    db.add(AuditEvent(actor=actor[:64], action="OUTBOUND_CONFIRMED_BY_SYNC", store_id=ob.store_id,
                      conversation_id=ob.conversation_id,
                      detail={"outbound_id": ob.id, "from_state": before, "source": ob.source, "job_id": ob.job_id,
                              "platform_message_id": msg.ebay_message_id,
                              "reason": "发送时结果不确定，eBay 同步拉回了正文一致的消息，确认已发出"}))


def dispatch_due(limit: int = 20) -> int:
    """Worker 调用：发出到期的待发消息。返回发出（或处理）的条数。"""
    now = utcnow()
    with session_scope() as db:
        for ob in db.scalars(select(OutboundMessage).where(OutboundMessage.state == "SENDING",
                                                           OutboundMessage.updated_at < now - STALE_SENDING)):
            ob.state, ob.error = "UNCERTAIN", "发送中途程序退出，不确定是否已发出，请到 eBay 核对"
            _audit(db, ob, "OUTBOUND_UNCERTAIN")
        ids = list(db.scalars(select(OutboundMessage.id).where(OutboundMessage.state == "PENDING",
                                                               OutboundMessage.send_after <= now)
                              .order_by(OutboundMessage.send_after).limit(limit)))
    done = 0
    for i in ids:
        with session_scope() as db:  # 先把状态改成 SENDING 并提交：撤回从这一刻起失效，程序中途退出也不会重发
            if db.execute(update(OutboundMessage).where(OutboundMessage.id == i, OutboundMessage.state == "PENDING")
                          .values(state="SENDING", updated_at=utcnow())).rowcount != 1:
                continue
        try:
            with session_scope() as db:
                deliver(db, db.get(OutboundMessage, i))
        except Exception as e:  # noqa: BLE001 — 不知道走到哪一步：当作不确定，不重发
            log.exception("发送待发消息 %s 出错", i)
            with session_scope() as db:
                ob = db.get(OutboundMessage, i)
                if ob.state == "SENDING":
                    ob.state, ob.error = "UNCERTAIN", f"发送出错（{type(e).__name__}），不确定是否已发出，请到 eBay 核对"
        done += 1
    return done


def outbox_dict(ob: OutboundMessage) -> dict:
    from ..api.deps import iso

    return {"id": ob.id, "conversation_id": ob.conversation_id, "body": ob.body, "zh_translation": ob.zh_translation,
            "source": ob.source, "actor": ob.actor, "state": ob.state, "send_after": iso(ob.send_after),
            "created_at": iso(ob.created_at), "sent_at": iso(ob.sent_at), "channel": ob.channel, "error": ob.error,
            "recalled_by": ob.recalled_by, "recalled_at": iso(ob.recalled_at), "job_id": ob.job_id,
            "has_media": bool((ob.meta or {}).get("media")),
            "media": [{"name": m.get("mediaName"), "file_id": m.get("fileId")} for m in (ob.meta or {}).get("media") or []]}


# ------------------------------------------------------------------ 内部
def _attempt(db: Session, intent: SendIntent | None, outcome: str, status: str) -> None:
    if intent is None:
        return
    n = len(db.scalars(select(SendAttempt).where(SendAttempt.intent_id == intent.id)).all()) + 1
    db.add(SendAttempt(intent_id=intent.id, attempt_no=n, outcome=outcome, response_status=status, completed_at=utcnow()))


def _audit(db: Session, ob: OutboundMessage, action: str) -> None:
    db.add(AuditEvent(actor="send_service", action=action, store_id=ob.store_id, conversation_id=ob.conversation_id,
                      detail={"outbound_id": ob.id, "source": ob.source, "channel": ob.channel, "error": ob.error,
                              "platform_message_id": ob.platform_message_id}))


def _undo(db: Session, ob: OutboundMessage, status: str, reason: str) -> None:
    """撤回或明确没发出：回复任务回到待审核、草稿可再改；各来源的状态回写。"""
    if ob.job_id:
        job = db.get(ReplyJob, ob.job_id)
        intent = db.scalar(select(SendIntent).where(SendIntent.job_id == ob.job_id))
        if intent is not None and intent.state in ("QUEUED", "SENDING"):
            intent.state, intent.last_error = "FAILED", reason[:500]  # FAILED 的意图可以重新提交
        if job is not None and job.state == JobState.SENDING:
            job.state = JobState.REVIEW_REQUIRED
            job.last_error = reason[:2000]
            job.lock_version += 1
            draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "APPROVED")
                              .order_by(Draft.id.desc()).limit(1))
            if draft is not None:
                draft.status = "ACTIVE"
    meta = ob.meta or {}
    if ob.source == "ticket" and meta.get("ticket_id"):
        from ..models import LogisticsTicket

        t = db.get(LogisticsTicket, meta["ticket_id"])
        if t is not None:
            t.send_status, t.send_error = status, reason[:500]
    elif ob.source == "logistics_notice":
        from ..models import LogisticsTicket

        for tid in meta.get("ticket_ids") or []:
            t = db.get(LogisticsTicket, tid)
            if t is not None:
                t.notice_status = status
    elif ob.source == "ack" and meta.get("ack_job_id"):
        job = db.get(ReplyJob, meta["ack_job_id"])
        if job is not None:
            job.ack_status = status
    elif ob.source == "ship_notice" and meta.get("notice_id"):
        from ..models import ShipNotice

        n = db.get(ShipNotice, meta["notice_id"])
        if n is not None:
            n.status, n.sent_at, n.sent_by = meta.get("prev_status") or n.status, None, None


def _on_sent(db: Session, ob: OutboundMessage, msg) -> None:
    meta = ob.meta or {}
    if ob.source == "ticket" and meta.get("ticket_id"):
        from ..models import LogisticsTicket

        t = db.get(LogisticsTicket, meta["ticket_id"])
        if t is not None:
            t.send_status, t.sent_at, t.sent_message_id, t.send_error = "SENT", ob.sent_at, msg.id, None
    elif ob.source == "ack" and meta.get("ack_job_id"):
        job = db.get(ReplyJob, meta["ack_job_id"])
        if job is not None:
            job.ack_status, job.ack_message_id = "SENT", msg.id
    elif ob.source == "ship_notice" and meta.get("notice_id"):
        from ..models import ShipNotice

        n = db.get(ShipNotice, meta["notice_id"])
        if n is not None:
            n.sent_message_id = msg.id
