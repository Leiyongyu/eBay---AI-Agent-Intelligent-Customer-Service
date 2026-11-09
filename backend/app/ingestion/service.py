"""消息接入：消息级去重、会话快照、合并窗口与回复任务（第 10 章“接入/处理”）。

- 按 (store_id, ebay_message_id) 去重，重复通知不新增消息。
- 合并窗口内连续来信：旧任务若尚未进入发送，标 CANCELLED_STALE，其覆盖的消息转入新任务；
  已进入 SENDING / SEND_UNCERTAIN / SENT 的任务不改写，新消息另建任务。
- 店铺自己发出的消息和 eBay 系统消息不建回复任务；系统消息转为会话事件并触发人工接管。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import (
    AuditEvent,
    Conversation,
    ConversationEvent,
    Draft,
    JobState,
    Message,
    ReplyJob,
    ReplyJobSource,
)


@dataclass
class IngestResult:
    conversation: Conversation
    message: Message
    job: ReplyJob | None
    duplicate: bool = False
    cancelled_job_ids: tuple[int, ...] = ()


def get_or_create_conversation(db: Session, store_id: str, ebay_conversation_id: str, buyer_key: str,
                               item_id: str | None, marketplace_id: str, marketplace_source: str | None) -> Conversation:
    conv = db.scalar(select(Conversation).where(Conversation.store_id == store_id,
                                                Conversation.ebay_conversation_id == ebay_conversation_id))
    if conv is None:
        conv = Conversation(store_id=store_id, ebay_conversation_id=ebay_conversation_id, buyer_key=buyer_key,
                            item_id=item_id, marketplace_id=marketplace_id or "UNKNOWN",
                            marketplace_source=marketplace_source, row_version=1)
        db.add(conv)
        db.flush()
    return conv


def _append_message(db: Session, conv: Conversation, ebay_message_id: str, role: str, direction: str,
                    body: str, sent_at: datetime | None) -> Message:
    seq = (db.scalar(select(func.max(Message.conversation_seq)).where(Message.conversation_id == conv.id)) or 0) + 1
    msg = Message(store_id=conv.store_id, ebay_message_id=ebay_message_id, conversation_id=conv.id,
                  conversation_seq=seq, sender_role=role, direction=direction, body=body,
                  sent_at=sent_at or utcnow())
    db.add(msg)
    conv.last_message_at = msg.sent_at
    conv.last_synced_at = utcnow()
    db.flush()
    return msg


def ingest_buyer_message(db: Session, *, conv: Conversation, ebay_message_id: str, body: str,
                         sent_at: datetime | None = None, skip_merge_window: bool = False,
                         actor: str = "ingestion") -> IngestResult:
    dup = db.scalar(select(Message).where(Message.store_id == conv.store_id, Message.ebay_message_id == ebay_message_id))
    if dup is not None:
        return IngestResult(conv, dup, None, duplicate=True)

    msg = _append_message(db, conv, ebay_message_id, "BUYER", "IN", body, sent_at)
    conv.row_version += 1

    # 合并窗口：同会话尚未发送的旧任务作废，消息归属转入新任务
    moved: list[str] = []
    cancelled: list[int] = []
    old_jobs = db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv.id,
                                                 ReplyJob.state.in_(JobState.REPLACEABLE))).all()
    for old in old_jobs:
        old.state = JobState.CANCELLED_STALE
        old.lock_version += 1
        cancelled.append(old.id)
        for d in db.scalars(select(Draft).where(Draft.job_id == old.id, Draft.status == "ACTIVE")):
            d.status = "SUPERSEDED"
        for src in db.scalars(select(ReplyJobSource).where(ReplyJobSource.job_id == old.id)):
            moved.append(src.source_message_id)
            db.delete(src)
    db.flush()

    window = 0 if skip_merge_window else get_settings().message_merge_window_seconds
    job = ReplyJob(store_id=conv.store_id, conversation_id=conv.id, anchor_source_message_id=ebay_message_id,
                   state=JobState.RECEIVED, ready_at=utcnow() + timedelta(seconds=window))
    db.add(job)
    db.flush()
    for mid in [*moved, ebay_message_id]:
        db.add(ReplyJobSource(store_id=conv.store_id, source_message_id=mid, job_id=job.id))
    db.add(AuditEvent(actor=actor, action="BUYER_MESSAGE_INGESTED", store_id=conv.store_id,
                      conversation_id=conv.id,
                      detail={"message_id": ebay_message_id, "job_id": job.id, "cancelled_jobs": cancelled,
                              "merge_window_seconds": window}))
    db.flush()
    return IngestResult(conv, msg, job, cancelled_job_ids=tuple(cancelled))


def open_job_for_unanswered(db: Session, conv: Conversation, actor: str = "ingestion") -> ReplyJob | None:
    """会话最后一条是买家消息、没有进行中 / 待审核的任务时，为最后一次店铺回复之后的买家消息建一个回复任务（规则：最后一句永远是我们的）。
    已有任务（排队、生成中、待审核、发送中）的不重复建；最后一条是我们发的不建。"""
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq)).all()
    if not msgs or msgs[-1].sender_role != "BUYER":
        return None
    open_states = (*JobState.REPLACEABLE, *JobState.PENDING_REVIEW, JobState.SENDING, JobState.SEND_UNCERTAIN)
    if db.scalar(select(ReplyJob.id).where(ReplyJob.conversation_id == conv.id, ReplyJob.state.in_(open_states)).limit(1)):
        return None
    last_seller = max((m.conversation_seq for m in msgs if m.sender_role == "SELLER"), default=0)
    pending = [m for m in msgs if m.sender_role == "BUYER" and m.conversation_seq > last_seller]
    anchor = pending[-1]
    if db.scalar(select(ReplyJobSource.id).where(ReplyJobSource.store_id == conv.store_id,
                                                 ReplyJobSource.source_message_id == anchor.ebay_message_id).limit(1)):
        return None  # 这条已经处理过（如审核员判定后结束）：不再建
    job = ReplyJob(store_id=conv.store_id, conversation_id=conv.id, anchor_source_message_id=anchor.ebay_message_id,
                   state=JobState.RECEIVED, ready_at=utcnow())
    db.add(job)
    db.flush()
    for m in pending:
        db.add(ReplyJobSource(store_id=conv.store_id, source_message_id=m.ebay_message_id, job_id=job.id))
    db.add(AuditEvent(actor=actor, action="UNANSWERED_JOB_OPENED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"job_id": job.id, "messages": len(pending), "last_buyer_at": anchor.sent_at.isoformat()
                              if anchor.sent_at else None}))
    db.flush()
    return job


def ingest_system_message(db: Session, *, conv: Conversation, ebay_message_id: str, body: str,
                          event_type: str, case_id: str | None = None) -> IngestResult:
    """eBay 系统消息不自动回复：落库为消息 + 会话事件，并让会话进入人工接管（第 8.1 节第 5 条）。"""
    dup = db.scalar(select(Message).where(Message.store_id == conv.store_id, Message.ebay_message_id == ebay_message_id))
    if dup is not None:
        return IngestResult(conv, dup, None, duplicate=True)
    msg = _append_message(db, conv, ebay_message_id, "SYSTEM", "IN", body, None)
    db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type=event_type,
                             source="ebay_system_message", case_id=case_id, detail=body[:1000]))
    conv.handoff_state = "HUMAN"
    conv.row_version += 1
    db.add(AuditEvent(actor="ingestion", action="SYSTEM_MESSAGE_INGESTED", store_id=conv.store_id,
                      conversation_id=conv.id, detail={"event_type": event_type, "case_id": case_id}))
    db.flush()
    return IngestResult(conv, msg, None)
