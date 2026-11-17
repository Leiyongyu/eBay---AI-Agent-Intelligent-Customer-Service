"""同事直接在 eBay 后台回复了买家：这个会话里还没发出的任务和 AI 草稿作废，免得客服再点发送、发出两条矛盾的回复。

判断规则：
- 只看 eBay 同步拉到的店铺消息（role=SELLER）。本系统发出的不算：消息 ID 带 SIM-OUT 前缀（模拟 / 回执），
  或已记在发件箱（outbound_messages.platform_message_id）、发送记录（send_intents.platform_message_id）里；
  这个会话发件箱里有正在发送（SENDING）的消息时也先不算（可能是本系统刚发出、还没来得及记下消息 ID）；
  发件箱里有“结果不确定”（UNCERTAIN：超时 / 5xx，没记下 eBay 消息 ID）的消息、且同步回来的正文与它一致
  （空白、换行归一后比较）、时间在发件前 10 分钟到之后 24 小时内：其实是本系统发出的——不关任务，
  并把那条发件箱补成 SENT、记下 eBay 消息 ID（outbox.confirm_from_sync）；
- 只作废“它覆盖的最后一条买家消息”早于这条店铺回复的任务：买家在店铺回复之后又来信的，照常处理；
- 作废的状态：RECEIVED、RETRYABLE_ERROR、REVIEW_REQUIRED、AUTO_ELIGIBLE、FAILED_HANDOFF → RESOLVED_NO_REPLY，
  原因加 REPLIED_ON_EBAY，活动草稿标 SUPERSEDED；正在生成（RUNNING）的由落库前的检查处理（agent/nodes.persist_candidate）；
- 发件箱里还在撤回期内、没发出的消息不动；整个过程不给买家发任何消息。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import AuditEvent, Conversation, ConversationEvent, Draft, JobState, Message, OutboundMessage, ReplyJob, SendIntent

REASON = "REPLIED_ON_EBAY"
CLOSABLE = (JobState.RECEIVED, JobState.RETRYABLE_ERROR, JobState.REVIEW_REQUIRED, JobState.AUTO_ELIGIBLE,
            JobState.FAILED_HANDOFF)
OWN_PREFIX = "SIM-OUT-"
MATCH_BEFORE, MATCH_AFTER = timedelta(minutes=10), timedelta(hours=24)  # 同步回来的消息时间相对发件的合理窗口
EBAY_TEXT_LIMIT = 2000  # sendMessage 只发前 2000 字（ebay/message_api.send_message）


def _norm(text: str | None) -> str:
    return " ".join((text or "")[:EBAY_TEXT_LIMIT].split())


def uncertain_outbox_for(db: Session, msg: Message) -> OutboundMessage | None:
    """发送结果不确定、没记下 eBay 消息 ID 的发件箱里，正文与这条店铺消息一致、时间对得上的那条（最早的）。"""
    body = _norm(msg.body)
    if not body or msg.conversation_id is None:
        return None
    for ob in db.scalars(select(OutboundMessage).where(
            OutboundMessage.conversation_id == msg.conversation_id, OutboundMessage.state.in_(("UNCERTAIN", "SENT")),
            OutboundMessage.platform_message_id.is_(None)).order_by(OutboundMessage.id)):
        if _norm(ob.body) != body:
            continue
        start = (ob.send_after or ob.created_at) - MATCH_BEFORE
        end = (ob.updated_at or ob.send_after or ob.created_at) + MATCH_AFTER
        if msg.sent_at is None or start <= msg.sent_at <= end:
            return ob
    return None


def _known_id(db: Session, mid: str | None) -> bool:
    """消息 ID 是本系统的：模拟前缀，或已记在发件箱 / 发送记录里。"""
    mid = mid or ""
    if mid.startswith(OWN_PREFIX):
        return True
    if mid and db.scalar(select(OutboundMessage.id).where(OutboundMessage.platform_message_id == mid).limit(1)):
        return True
    return bool(mid and db.scalar(select(SendIntent.id).where(SendIntent.platform_message_id == mid).limit(1)))


def is_own(db: Session, msg: Message) -> bool:
    """这条店铺消息是不是本系统发出的。"""
    if _known_id(db, msg.ebay_message_id):
        return True
    # 本系统正在往这个会话发消息（已调 eBay、还没记下消息 ID）：先不当成后台回复
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.conversation_id == msg.conversation_id,
                                                  OutboundMessage.state == "SENDING").limit(1)):
        return True
    # 发送超时、结果不确定，但同步拉回了正文一致的消息：其实是本系统发出的
    return uncertain_outbox_for(db, msg) is not None


def anchor_time(db: Session, job: ReplyJob) -> datetime | None:
    """任务覆盖的最后一条买家消息的时间（找不到消息时用任务创建时间）。"""
    t = db.scalar(select(Message.sent_at).where(Message.conversation_id == job.conversation_id,
                                                Message.ebay_message_id == job.anchor_source_message_id).limit(1))
    return t or job.created_at


def outside_reply_after(db: Session, conversation_id: int, after: datetime | None) -> Message | None:
    """这个会话里晚于 after 的、不是本系统发出的店铺回复（取最早的一条）。"""
    q = select(Message).where(Message.conversation_id == conversation_id, Message.sender_role == "SELLER")
    if after is not None:
        q = q.where(Message.sent_at > after)
    for m in db.scalars(q.order_by(Message.sent_at, Message.id)):
        if not is_own(db, m):
            return m
    return None


def close_job(db: Session, job: ReplyJob, reply: Message, *, actor: str) -> None:
    """把任务关掉（不发任何消息）：状态 RESOLVED_NO_REPLY、原因 REPLIED_ON_EBAY、活动草稿作废、记审计和会话事件。"""
    before = job.state
    job.state = JobState.RESOLVED_NO_REPLY
    job.decision_reasons = list(dict.fromkeys([*(job.decision_reasons or []), REASON]))
    job.last_error = None
    job.lock_version += 1
    drafts = 0
    for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")):
        d.status = "SUPERSEDED"
        drafts += 1
    when = reply.sent_at.strftime("%m-%d %H:%M") if reply.sent_at else "刚才"
    db.add(AuditEvent(actor=actor[:64], action="JOB_CLOSED_REPLIED_ON_EBAY", store_id=job.store_id,
                      conversation_id=job.conversation_id,
                      detail={"job_id": job.id, "from_state": before, "seller_message_id": reply.ebay_message_id,
                              "seller_sent_at": reply.sent_at.isoformat() if reply.sent_at else None, "drafts": drafts}))
    db.add(ConversationEvent(store_id=job.store_id, conversation_id=job.conversation_id, event_type=REASON,
                             source=actor[:64], detail=f"已在 eBay 后台回复（{when}），系统草稿作废（任务 #{job.id}）",
                             occurred_at=reply.sent_at or utcnow()))


def close_replied_jobs(db: Session, conv: Conversation, reply: Message, *, actor: str = "ebay_sync") -> list[int]:
    """拉到一条店铺回复后调用：关掉这个会话里被它回复掉的、还没发出的任务。本系统发出的回复不触发。返回关掉的任务 ID。"""
    if reply.sender_role != "SELLER":
        return []
    ob = None if _known_id(db, reply.ebay_message_id) else uncertain_outbox_for(db, reply)
    if ob is not None:  # 本系统发送时结果不确定、其实发出去了：补成已发出，不关任务
        from .sending.outbox import confirm_from_sync

        confirm_from_sync(db, ob, reply, actor=actor)
        return []
    if is_own(db, reply):
        return []
    closed = []
    for job in db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv.id, ReplyJob.state.in_(CLOSABLE))
                          .order_by(ReplyJob.id)):
        at = anchor_time(db, job)
        if reply.sent_at is None or at is None or at < reply.sent_at:
            close_job(db, job, reply, actor=actor)
            closed.append(job.id)
    return closed


def stale_pending(db: Session, store_ids: list[str] | None = None) -> list[dict]:
    """存量检查（只读）：还挂在待处理、但已经有晚于它最后一条买家消息的店铺后台回复的任务。"""
    q = select(ReplyJob).where(ReplyJob.state.in_(CLOSABLE))
    if store_ids:
        q = q.where(ReplyJob.store_id.in_(store_ids))
    out = []
    for job in db.scalars(q.order_by(ReplyJob.id)):
        reply = outside_reply_after(db, job.conversation_id, anchor_time(db, job))
        if reply is None:
            continue
        conv = db.get(Conversation, job.conversation_id)
        out.append({"job_id": job.id, "state": job.state, "store_id": job.store_id, "conversation_id": job.conversation_id,
                    "buyer": conv.buyer_key if conv else None, "buyer_at": anchor_time(db, job),
                    "seller_at": reply.sent_at, "seller_message_id": reply.ebay_message_id,
                    "seller_preview": (reply.body or "")[:80]})
    return out
