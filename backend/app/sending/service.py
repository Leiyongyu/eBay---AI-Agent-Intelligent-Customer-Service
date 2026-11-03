"""受控发送服务（第 10 章“发送”、第 17.2 节 SendService）。

- 人工批准与（P3）自动发送走同一入口：先在事务里创建唯一发送意图（send_intents.job_id 唯一），
  返回意图 ID；实际外发由 Worker 异步执行，不在请求里同步等待。
- 每个回复任务最多一个外发意图；同一幂等键重复提交返回同一意图。
- 外发前再次执行合规检查，BLOCK 不可绕过。
- 请求超时/崩溃无法确认结果时标记 SEND_UNCERTAIN，冻结并等待对账，不自动重试。
"""
from __future__ import annotations

import difflib
import hashlib
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..guards.compliance import ComplianceChecker
from ..ingestion.service import _append_message
from ..models import (AuditEvent, Conversation, Draft, JobState, Message, ReplyJob, ReviewFeedback, SendAttempt,
                      SendIntent)
from .channel import ChannelNotConfigured, ChannelUncertain, get_channel


class SendError(Exception):
    def __init__(self, status_code: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail or {}


def edit_ratio(original: str, final: str) -> float:
    if not original:
        return 1.0
    return round(1 - difflib.SequenceMatcher(None, original, final).ratio(), 4)


def request_send(db: Session, *, job: ReplyJob, draft: Draft, final_text: str, actor: str, idempotency_key: str,
                 expected_conversation_version: int, reason_codes: list[str] | None = None,
                 handling_ms: int | None = None) -> SendIntent:
    conv = db.get(Conversation, job.conversation_id)
    if conv.channel_source == "ebay":
        # 只读对接阶段：真实 eBay 会话的发送接口还没接入，不能让审核员误以为已回复买家
        raise SendError(409, "这是从 eBay 拉取的真实会话，当前为只读阶段，本系统暂不能向 eBay 发送回复，请到 eBay 后台回复")

    existing = db.scalar(select(SendIntent).where(SendIntent.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.job_id != job.id:
            raise SendError(409, "幂等键已用于其他任务")
        return existing  # 重复点击/重放：返回同一意图

    prior = db.scalar(select(SendIntent).where(SendIntent.job_id == job.id))
    if prior is not None and prior.state != "FAILED":
        raise SendError(409, "该回复任务已经提交过发送，不能再生成第二个外发意图")
    if job.state not in JobState.PENDING_REVIEW:
        raise SendError(409, f"任务状态为 {job.state}，不能发送")

    if conv.row_version != expected_conversation_version:
        raise SendError(409, "会话有新消息或状态已变化，请刷新后重新确认", {"current_version": conv.row_version})
    if draft.author == "agent" and draft.conversation_row_version != conv.row_version:
        raise SendError(409, "草稿基于旧的会话版本（期间有新消息或系统事件），请重新生成或人工撰写")

    text = final_text.strip()
    if not text:
        raise SendError(422, "回复内容为空")
    compliance = ComplianceChecker().check(text)
    if compliance.result == "BLOCK":
        raise SendError(422, "合规硬阻断：请修改命中内容后再提交（不可跳过）", compliance.to_dict())

    ratio = edit_ratio(draft.text, text) if draft.author == "agent" else None
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if prior is not None:
        # 上次明确未发出（FAILED）：复用同一个意图行，保证一个任务始终只有一个外发意图
        intent = prior
        intent.draft_id, intent.idempotency_key, intent.actor = draft.id, idempotency_key, actor
        intent.text, intent.content_hash, intent.state, intent.last_error = text, content_hash, "QUEUED", None
    else:
        intent = SendIntent(job_id=job.id, draft_id=draft.id, idempotency_key=idempotency_key, actor=actor,
                            text=text, content_hash=content_hash, state="QUEUED")
        db.add(intent)
    draft.status = "APPROVED"
    job.state = JobState.SENDING
    job.lock_version += 1
    if draft.author == "agent":
        action = "APPROVE" if ratio == 0 else "EDIT_APPROVE"
    else:
        action = "MANUAL"
    db.add(ReviewFeedback(draft_id=draft.id, job_id=job.id, reviewer=actor, action=action,
                          reason_codes=reason_codes or [], edit_ratio=ratio, handling_ms=handling_ms))
    db.add(AuditEvent(actor=actor, action="SEND_REQUESTED", store_id=job.store_id, conversation_id=job.conversation_id,
                      detail={"job_id": job.id, "draft_id": draft.id, "edit_ratio": ratio,
                              "compliance": compliance.result}))
    db.flush()
    return intent


def dispatch(db: Session, intent: SendIntent) -> None:
    """Worker 调用：锁定意图 → 复核 → 外发 → 记录结果。"""
    job = db.get(ReplyJob, intent.job_id)
    conv = db.get(Conversation, job.conversation_id)
    attempt_no = len(db.scalars(select(SendAttempt).where(SendAttempt.intent_id == intent.id)).all()) + 1
    attempt = SendAttempt(intent_id=intent.id, attempt_no=attempt_no)
    db.add(attempt)
    intent.state = "SENDING"
    db.flush()

    channel = get_channel()
    try:
        receipt = channel.send(db, conv, intent.text)
    except ChannelNotConfigured as e:
        attempt.outcome, attempt.response_status = "NOT_SENT", "not_configured"
        intent.state, intent.last_error = "FAILED", str(e)
        job.state = JobState.REVIEW_REQUIRED  # 明确未发出：回到待审，人工可重新提交
        db.get(Draft, intent.draft_id).status = "ACTIVE"
    except ChannelUncertain as e:
        attempt.outcome, attempt.response_status = "UNCERTAIN", "timeout"
        intent.state, intent.last_error = "SEND_UNCERTAIN", str(e)
        job.state = JobState.SEND_UNCERTAIN
    else:
        attempt.outcome, attempt.response_status = "SENT", receipt.http_status
        intent.state, intent.platform_message_id, intent.sent_at = "SENT", receipt.platform_message_id, utcnow()
        job.state = JobState.SENT
    attempt.completed_at = utcnow()
    job.lock_version += 1
    db.add(AuditEvent(actor="send_service", action=f"SEND_{attempt.outcome}", store_id=job.store_id,
                      conversation_id=job.conversation_id,
                      detail={"intent_id": intent.id, "channel": channel.name, "error": intent.last_error}))
    db.flush()


SIMULATED_PREFIX = "SIM-OUT-"
GENERATING = (JobState.RECEIVED, JobState.RUNNING, JobState.SENDING, JobState.SEND_UNCERTAIN)
AUTO_ACTOR = "AI自动回复"  # 通过闸门后由 Agent 自动发出（仍是模拟发送）时记录的发送人


def auto_reply_message_ids(db: Session, platform_message_ids: list[str]) -> set[str]:
    """这些平台消息 ID 里，哪些是 AI 自动回复发出的。"""
    if not platform_message_ids:
        return set()
    return set(db.scalars(select(SendIntent.platform_message_id).where(
        SendIntent.platform_message_id.in_(platform_message_ids), SendIntent.actor == AUTO_ACTOR)))


def auto_reply_streak(db: Session, conv_id: int) -> int:
    """会话末尾连续几条店铺回复是 AI 自动回复（遇到人工回复就停）。"""
    outs = list(db.scalars(select(Message.ebay_message_id).where(Message.conversation_id == conv_id,
                                                                 Message.direction == "OUT")
                           .order_by(Message.conversation_seq.desc()).limit(20)))
    autos = auto_reply_message_ids(db, outs)
    n = 0
    for mid in outs:
        if mid not in autos:
            break
        n += 1
    return n


def last_reply_was_auto(db: Session, conv_id: int) -> bool:
    return auto_reply_streak(db, conv_id) > 0


def simulate_send(db: Session, *, conv: Conversation, text: str, actor: str,
                  handling_ms: int | None = None, text_zh: str | None = None,
                  auto: bool = False) -> tuple[Message, ReplyJob | None]:
    """工作台“发送”：模拟发送，只在本地会话追加一条店铺消息（平台消息 ID 以 SIM-OUT- 开头），绝不调用 eBay。

    有待审任务时一并结案：AI 草稿记为已批准并记录修改比例（没有草稿则记为人工撰写），
    写一条状态为 SENT 的发送意图，用它把这条消息和生成依据关联起来。合规 BLOCK 同样不能发送。
    """
    if conv.conversation_type == "FROM_EBAY":
        raise SendError(409, "eBay 系统消息会话不能回复")
    text = text.strip()
    if not text:
        raise SendError(422, "回复内容为空")
    compliance = ComplianceChecker().check(text)
    if compliance.result == "BLOCK":
        raise SendError(422, "合规硬阻断：请修改命中内容后再发送（不可跳过）", compliance.to_dict())

    job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == conv.id,
                                           ReplyJob.state != JobState.CANCELLED_STALE)
                    .order_by(ReplyJob.id.desc()).limit(1))
    if job is not None and job.state in GENERATING:
        raise SendError(409, "AI 正在生成草稿，请等生成完成后再发送")
    if job is not None and job.state not in (*JobState.PENDING_REVIEW, JobState.RETRYABLE_ERROR):
        job = None  # 上一个任务已结束：这是一条与 AI 任务无关的人工消息

    mid = f"{SIMULATED_PREFIX}{uuid.uuid4().hex[:12]}"
    ratio = None
    draft_zh = None
    if job is not None:
        draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")
                          .order_by(Draft.id.desc()).limit(1))
        if draft is not None and draft.author == "agent" and draft.conversation_row_version != conv.row_version:
            raise SendError(409, "草稿生成后会话有新消息，请重新生成草稿或清空后人工撰写")
        if draft is None:
            version = (db.scalar(select(func.max(Draft.version)).where(Draft.job_id == job.id)) or 0) + 1
            draft = Draft(job_id=job.id, run_generation=job.run_generation, version=version, author=actor,
                          conversation_row_version=conv.row_version, text=text, risk_level=job.risk_level,
                          decision="REVIEW_REQUIRED", review_reasons=["MANUAL_REPLY"])
            db.add(draft)
            db.flush()
        ratio = edit_ratio(draft.text, text) if draft.author == "agent" else None
        if ratio == 0:  # 原样发出 AI 草稿：草稿自带的中文就是这条消息的中文
            draft_zh = draft.text_zh
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        intent = db.scalar(select(SendIntent).where(SendIntent.job_id == job.id))
        if intent is None:
            intent = SendIntent(job_id=job.id)
            db.add(intent)
        intent.draft_id, intent.idempotency_key, intent.actor = draft.id, f"sim-{uuid.uuid4().hex}", actor
        intent.text, intent.content_hash, intent.state = text, content_hash, "SENT"
        intent.platform_message_id, intent.last_error, intent.sent_at = mid, None, utcnow()
        db.flush()
        attempt_no = len(db.scalars(select(SendAttempt).where(SendAttempt.intent_id == intent.id)).all()) + 1
        db.add(SendAttempt(intent_id=intent.id, attempt_no=attempt_no, outcome="SENT", response_status="simulated",
                           completed_at=utcnow()))
        for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE", Draft.id != draft.id)):
            d.status = "SUPERSEDED"
        draft.status = "APPROVED"
        job.state = JobState.SENT
        job.lock_version += 1
        if not auto:  # 自动回复没有人审核，不计入审核反馈与效率指标
            action = "MANUAL" if draft.author != "agent" else ("APPROVE" if ratio == 0 else "EDIT_APPROVE")
            db.add(ReviewFeedback(draft_id=draft.id, job_id=job.id, reviewer=actor, action=action,
                                  reason_codes=[], edit_ratio=ratio, handling_ms=handling_ms))

    msg = _append_message(db, conv, mid, "SELLER", "OUT", text, None)
    # 中文只供审核员对照，买家收到的始终是 text 原文
    msg.zh_translation = (text_zh or "").strip() or draft_zh
    conv.row_version += 1
    db.add(AuditEvent(actor=actor, action="AUTO_SIMULATED_SEND" if auto else "SIMULATED_SEND",
                      store_id=conv.store_id, conversation_id=conv.id,
                      detail={"message_id": mid, "job_id": job.id if job else None, "edit_ratio": ratio,
                              "compliance": compliance.result}))
    db.flush()
    return msg, job
