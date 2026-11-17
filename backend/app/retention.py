"""聊天记录保留期：每个店铺只保留最近 N 天的聊天记录，更早的每天轮询清理（第 18.2 节“数据保留”）。

规则（按消息在 eBay 的发送时间 sent_at）：
- 早于“现在 − N 天”的消息删除，连同它的附件记录、图片观察和磁盘上的图片副本；
- 早于截止时间、已结束的回复任务删除，连同草稿、发送记录、Agent 运行与步骤、工具调用、审核反馈；
- 早于截止时间的会话事件删除；消息全部删光、且创建早于截止时间的会话删除；
- 还没处理完的会话整段跳过，处理完后下一轮再删：有未结束的回复任务、发送中或结果不确定的发送、未过期的认领；
- 审计日志不含聊天正文：同步流水（EBAY_SYNC）按 N 天删，其他按 AUDIT_RETENTION_DAYS 删；
- N 取店铺的 stores.retention_days，空则用 DATA_RETENTION_DAYS；0 表示不清理。
- 上线前基线消息（baseline_messages / baseline_conversations，见 baseline.py）按 BASELINE_RETENTION_DAYS 一起清理。
拉取 eBay 消息时也跳过早于截止时间的消息（ebay/sync.py），否则会话来新消息时旧消息会被整段拉回来。

执行：分批删除（RETENTION_BATCH_SIZE 条消息一批，每批单独提交，不长时间锁表）；与 eBay 同步互斥；
RETENTION_DRY_RUN=true 时只统计不删除。每次运行记一条审计（只有数量）。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time as dtime, timedelta, timezone

from sqlalchemy import delete, exists, func, select, true, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import session_scope, utcnow
from .models import (
    AgentRun,
    AgentStep,
    AuditEvent,
    Conversation,
    ConversationEvent,
    Draft,
    ImageObservation,
    JobState,
    Message,
    MessageAttachment,
    ConversationMark,
    InvoiceRequest,
    LogisticsTicket,
    ReplyJob,
    ReplyJobSource,
    ReviewFeedback,
    SendAttempt,
    SendIntent,
    Store,
    ToolCall,
    WorkClaim,
)

log = logging.getLogger(__name__)

FINISHED_JOB_STATES = (JobState.SENT, JobState.CANCELLED_STALE, JobState.RESOLVED_NO_REPLY)
OPEN_SEND_STATES = ("QUEUED", "SENDING", "SEND_UNCERTAIN")
RUN_ACTIONS = ("RETENTION_CLEANUP", "RETENTION_DRY_RUN")
SCHEDULER_ACTOR = "retention_scheduler"
COUNT_KEYS = ("messages", "attachments", "files", "jobs", "events", "conversations")


# ---------------------------------------------------------------- 截止时间
def store_days(store: Store, s: Settings | None = None) -> int:
    s = s or get_settings()
    return s.data_retention_days if store.retention_days is None else store.retention_days


def cutoff_for(store: Store, now: datetime | None = None, s: Settings | None = None) -> datetime | None:
    """该店铺的保留截止时间（UTC，naive）；不清理时返回 None。"""
    days = store_days(store, s)
    return None if days <= 0 else (now or utcnow()) - timedelta(days=days)


def store_cutoff(db: Session, store_id: str) -> datetime | None:
    store = db.get(Store, store_id)
    return cutoff_for(store) if store else None


# ---------------------------------------------------------------- 查询条件
def _protected(db: Session, store_id: str, now: datetime) -> set[int]:
    """还没处理完的会话：整段跳过。"""
    busy = db.scalars(select(ReplyJob.conversation_id).where(
        ReplyJob.store_id == store_id, ReplyJob.state.not_in(FINISHED_JOB_STATES))).all()
    sending = db.scalars(select(ReplyJob.conversation_id).join(SendIntent, SendIntent.job_id == ReplyJob.id).where(
        ReplyJob.store_id == store_id, SendIntent.state.in_(OPEN_SEND_STATES))).all()
    claimed = db.scalars(select(WorkClaim.conversation_id).where(
        WorkClaim.store_id == store_id, WorkClaim.expires_at > now)).all()
    return set(busy) | set(sending) | set(claimed)


def _old_messages(store_id: str, cutoff: datetime, protected: set[int]):
    return (Message.store_id == store_id, Message.sent_at < cutoff, Message.conversation_id.not_in(list(protected)))


def _old_jobs(store_id: str, cutoff: datetime, protected: set[int]):
    return (ReplyJob.store_id == store_id, ReplyJob.created_at < cutoff, ReplyJob.state.in_(FINISHED_JOB_STATES),
            ReplyJob.conversation_id.not_in(list(protected)))


def _old_events(store_id: str, cutoff: datetime, protected: set[int]):
    return (ConversationEvent.store_id == store_id, ConversationEvent.occurred_at < cutoff,
            ConversationEvent.conversation_id.not_in(list(protected)))


def _empty_conversations(store_id: str, cutoff: datetime, protected: set[int], after_delete: bool):
    """清理后不再有任何消息的会话。试运行时按“没有截止时间之后的消息”估算。"""
    recent = Message.sent_at >= cutoff if not after_delete else true()
    has_msg = exists().where(Message.conversation_id == Conversation.id, recent)
    return (Conversation.store_id == store_id, Conversation.created_at < cutoff,
            Conversation.id.not_in(list(protected)), ~has_msg)


def _count(db: Session, model, conds) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0


# ---------------------------------------------------------------- 删除
def _delete_jobs(db: Session, job_ids: list[int]) -> int:
    if not job_ids:
        return 0
    run_ids = select(AgentRun.id).where(AgentRun.job_id.in_(job_ids))
    intent_ids = select(SendIntent.id).where(SendIntent.job_id.in_(job_ids))
    db.execute(delete(AgentStep).where(AgentStep.run_id.in_(run_ids)))
    db.execute(delete(AgentRun).where(AgentRun.job_id.in_(job_ids)))
    db.execute(delete(SendAttempt).where(SendAttempt.intent_id.in_(intent_ids)))
    db.execute(delete(ReviewFeedback).where(ReviewFeedback.job_id.in_(job_ids)))  # 引用草稿，先删
    db.execute(delete(SendIntent).where(SendIntent.job_id.in_(job_ids)))  # 引用草稿，先删
    db.execute(delete(ToolCall).where(ToolCall.job_id.in_(job_ids)))
    db.execute(delete(Draft).where(Draft.job_id.in_(job_ids)))
    db.execute(delete(ReplyJobSource).where(ReplyJobSource.job_id.in_(job_ids)))
    return db.execute(delete(ReplyJob).where(ReplyJob.id.in_(job_ids))).rowcount or 0


def _delete_attachments(db: Session, conds) -> tuple[int, set[str]]:
    rows = db.execute(select(MessageAttachment.id, MessageAttachment.local_path).where(*conds)).all()
    if not rows:
        return 0, set()
    ids = [r[0] for r in rows]
    db.execute(delete(ImageObservation).where(ImageObservation.attachment_id.in_(ids)))
    db.execute(delete(MessageAttachment).where(MessageAttachment.id.in_(ids)))
    return len(ids), {r[1] for r in rows if r[1]}


def _delete_files(paths: set[str]) -> int:
    """删除已没有任何附件记录引用的图片副本（同一图片可能被多条附件共用）。"""
    if not paths:
        return 0
    root = get_settings().media_root.resolve()
    removed = 0
    with session_scope() as db:
        still_used = set(db.scalars(select(MessageAttachment.local_path)
                                    .where(MessageAttachment.local_path.in_(list(paths)))).all())
    for rel in paths - still_used:
        path = (root / rel).resolve()
        if root in path.parents and path.exists():  # 防路径穿越
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def delete_conversations(db: Session, conv_ids: list[int]) -> tuple[dict, set[str]]:
    """整段删除会话及其消息、附件、回复任务、事件与认领（不管保留期）。返回 (数量, 待删的图片文件)；文件在提交后用 _delete_files 删。"""
    if not conv_ids:
        return {"conversations": 0, "messages": 0, "attachments": 0, "jobs": 0}, set()
    n_atts, files = _delete_attachments(db, (MessageAttachment.conversation_id.in_(conv_ids),))
    n_jobs = _delete_jobs(db, list(db.scalars(select(ReplyJob.id).where(ReplyJob.conversation_id.in_(conv_ids)))))
    db.execute(delete(ConversationEvent).where(ConversationEvent.conversation_id.in_(conv_ids)))
    db.execute(delete(WorkClaim).where(WorkClaim.conversation_id.in_(conv_ids)))
    db.execute(delete(ConversationMark).where(ConversationMark.conversation_id.in_(conv_ids)))
    # 发票申请（含买家邮箱）随会话删；已生成的发票留底不删（invoices 表，财务要求保存）
    db.execute(delete(InvoiceRequest).where(InvoiceRequest.conversation_id.in_(conv_ids)))
    # 飞书售后工单随会话删（卡片和话题留在飞书群里）
    from .models import AftersalesCase

    db.execute(delete(AftersalesCase).where(AftersalesCase.conversation_id.in_(conv_ids)))
    # 发件箱记录随会话删（还没发出的也一起删掉，不会再发）
    from .models import OutboundMessage

    db.execute(delete(OutboundMessage).where(OutboundMessage.conversation_id.in_(conv_ids)))
    # 谷仓查件单保留（来自谷仓），只解除与会话的关联；之后同步时按买家重新匹配
    db.execute(update(LogisticsTicket).where(LogisticsTicket.conversation_id.in_(conv_ids)).values(conversation_id=None))
    n_msgs = db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids))).rowcount or 0
    n_convs = db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids))).rowcount or 0
    return {"conversations": n_convs, "messages": n_msgs, "attachments": n_atts, "jobs": n_jobs}, files


def _clean_store(store_id: str, cutoff: datetime, now: datetime, batch: int, dry_run: bool) -> dict:
    with session_scope() as db:
        protected = _protected(db, store_id, now)
        if dry_run:
            return {
                "cutoff": cutoff.isoformat(timespec="seconds") + "Z",
                "protected_conversations": len(protected),
                "messages": _count(db, Message, _old_messages(store_id, cutoff, protected)),
                "attachments": db.scalar(select(func.count()).select_from(MessageAttachment).join(
                    Message, Message.id == MessageAttachment.message_id).where(
                    *_old_messages(store_id, cutoff, protected))) or 0,
                "files": None,
                "jobs": _count(db, ReplyJob, _old_jobs(store_id, cutoff, protected)),
                "events": _count(db, ConversationEvent, _old_events(store_id, cutoff, protected)),
                "conversations": _count(db, Conversation, _empty_conversations(store_id, cutoff, protected, False)),
            }

    r = {"cutoff": cutoff.isoformat(timespec="seconds") + "Z", "protected_conversations": len(protected),
         **{k: 0 for k in COUNT_KEYS}}
    paths: set[str] = set()

    # 1. 旧消息：分批删除，连同附件、图片观察、任务来源
    while True:
        with session_scope() as db:
            rows = db.execute(select(Message.id, Message.ebay_message_id)
                              .where(*_old_messages(store_id, cutoff, protected))
                              .order_by(Message.id).limit(batch)).all()
            if not rows:
                break
            ids, ebay_ids = [x[0] for x in rows], [x[1] for x in rows]
            n, p = _delete_attachments(db, (MessageAttachment.message_id.in_(ids),))
            r["attachments"] += n
            paths |= p
            db.execute(delete(ReplyJobSource).where(ReplyJobSource.store_id == store_id,
                                                    ReplyJobSource.source_message_id.in_(ebay_ids)))
            r["messages"] += db.execute(delete(Message).where(Message.id.in_(ids))).rowcount or 0

    # 2. 已结束的旧回复任务
    while True:
        with session_scope() as db:
            job_ids = list(db.scalars(select(ReplyJob.id).where(*_old_jobs(store_id, cutoff, protected))
                                      .order_by(ReplyJob.id).limit(batch)))
            if not job_ids:
                break
            r["jobs"] += _delete_jobs(db, job_ids)

    # 3. 旧会话事件
    with session_scope() as db:
        r["events"] += db.execute(delete(ConversationEvent).where(
            *_old_events(store_id, cutoff, protected))).rowcount or 0

    # 4. 消息已删光的旧会话
    while True:
        with session_scope() as db:
            conv_ids = list(db.scalars(select(Conversation.id).where(
                *_empty_conversations(store_id, cutoff, protected, True)).order_by(Conversation.id).limit(batch)))
            if not conv_ids:
                break
            n, p = _delete_attachments(db, (MessageAttachment.conversation_id.in_(conv_ids),))
            r["attachments"] += n
            paths |= p
            r["jobs"] += _delete_jobs(db, list(db.scalars(select(ReplyJob.id).where(
                ReplyJob.conversation_id.in_(conv_ids)))))
            r["events"] += db.execute(delete(ConversationEvent).where(
                ConversationEvent.conversation_id.in_(conv_ids))).rowcount or 0
            db.execute(delete(WorkClaim).where(WorkClaim.conversation_id.in_(conv_ids)))
            db.execute(delete(ConversationMark).where(ConversationMark.conversation_id.in_(conv_ids)))
            r["conversations"] += db.execute(delete(Conversation).where(
                Conversation.id.in_(conv_ids))).rowcount or 0

    r["files"] = _delete_files(paths)
    return r


def _audit_conditions(s: Settings, now: datetime) -> list[tuple[str, tuple]]:
    out = []
    if s.data_retention_days > 0:  # 同步流水每 5 分钟每店铺两条，增长最快，跟聊天记录同一保留期
        out.append(("sync", (AuditEvent.action == "EBAY_SYNC",
                             AuditEvent.created_at < now - timedelta(days=s.data_retention_days))))
    if s.audit_retention_days > 0:
        out.append(("other", (AuditEvent.action != "EBAY_SYNC",
                              AuditEvent.created_at < now - timedelta(days=s.audit_retention_days))))
    return out


# ---------------------------------------------------------------- 入口
PUSH_RECORD_DAYS = 7
_lock = threading.Lock()


def run(dry_run: bool | None = None, actor: str = "system", now: datetime | None = None,
        lock_timeout: float = 600) -> dict:
    """清理所有店铺；返回各店铺、各类数据的删除（或试运行时将删除）数量。"""
    from .ebay.sync import _run_lock as sync_lock  # 与 eBay 同步互斥，避免边拉边删
    from .media.service import cleanup_expired

    s = get_settings()
    dry = s.retention_dry_run if dry_run is None else dry_run
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "上一次清理还在进行中"}
    try:
        if not sync_lock.acquire(timeout=lock_timeout):
            return {"ok": False, "error": "eBay 同步一直在进行，稍后再试"}
        try:
            now = now or utcnow()
            started = utcnow()
            result: dict = {"ok": True, "dry_run": dry, "started_at": started.isoformat(timespec="seconds") + "Z",
                            "stores": {}}
            with session_scope() as db:
                stores = [(st.id, store_days(st, s), cutoff_for(st, now, s)) for st in db.scalars(select(Store))]
            for store_id, days, cutoff in stores:
                if cutoff is None:
                    result["stores"][store_id] = {"days": days, "skipped": "不清理（保留天数为 0）"}
                    continue
                try:
                    result["stores"][store_id] = {"days": days, **_clean_store(
                        store_id, cutoff, now, max(1, s.retention_batch_size), dry)}
                except Exception as e:  # 一个店铺失败不影响其他店铺；已提交的批次保留
                    log.exception("店铺 %s 清理失败", store_id)
                    result["ok"] = False
                    result["stores"][store_id] = {"days": days, "error": f"{type(e).__name__}: {str(e)[:300]}"}
            result["totals"] = {k: sum((v.get(k) or 0) for v in result["stores"].values()) for k in COUNT_KEYS}

            with session_scope() as db:
                audit = {}
                for name, conds in _audit_conditions(s, now):
                    audit[name] = _count(db, AuditEvent, conds) if dry else \
                        db.execute(delete(AuditEvent).where(*conds)).rowcount or 0
                result["audit_events"] = audit
                # 推送收件记录只用于排查，保留 7 天
                from .models import EbayNotification

                pushes = (EbayNotification.received_at < now - timedelta(days=PUSH_RECORD_DAYS),)
                result["push_notifications"] = _count(db, EbayNotification, pushes) if dry else                     db.execute(delete(EbayNotification).where(*pushes)).rowcount or 0
                result["media_expired"] = None if dry else cleanup_expired(db)
            try:  # 上线前基线消息（baseline_messages，含买家正文）：按 BASELINE_RETENTION_DAYS 清理
                from .baseline import cleanup as baseline_cleanup

                result["baseline"] = baseline_cleanup(now, dry)
            except Exception as e:
                log.exception("基线消息清理失败")
                result["ok"] = False
                result["baseline"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            result["finished_at"] = utcnow().isoformat(timespec="seconds") + "Z"
            with session_scope() as db:
                db.add(AuditEvent(actor=actor, action="RETENTION_DRY_RUN" if dry else "RETENTION_CLEANUP",
                                  detail=result))
            log.info("聊天记录%s：%s", "清理试运行" if dry else "清理", result["totals"])
            return result
        finally:
            sync_lock.release()
    finally:
        _lock.release()


def last_runs(db: Session, limit: int = 5) -> list[dict]:
    rows = db.scalars(select(AuditEvent).where(AuditEvent.action.in_(RUN_ACTIONS))
                      .order_by(AuditEvent.id.desc()).limit(limit)).all()
    return [{"actor": a.actor, "at": a.created_at.isoformat(timespec="seconds") + "Z", **(a.detail or {})}
            for a in rows]


# ---------------------------------------------------------------- 定时
def parse_clock(value: str) -> dtime:
    try:
        hh, mm = value.strip().split(":")
        return dtime(int(hh), int(mm))
    except Exception:
        log.warning("RETENTION_CLEANUP_TIME=%s 格式不对（应为 HH:MM），改用 03:30", value)
        return dtime(3, 30)


def last_due(now_local: datetime, at: dtime) -> datetime:
    """最近一个已经过去的计划时间（本机时间）。"""
    today = now_local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return today if now_local >= today else today - timedelta(days=1)


class RetentionScheduler:
    """每 10 分钟检查一次：最近一个计划时间之后还没按计划跑过，就跑一次（当时没开机的，启动后补跑）。"""

    CHECK_SECONDS = 600
    STARTUP_DELAY_SECONDS = 60

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict | None = None

    def start(self) -> bool:
        if get_settings().data_retention_days <= 0 and not self._any_store_override():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="retention", daemon=True)
        self._thread.start()
        return True

    @staticmethod
    def _any_store_override() -> bool:
        with session_scope() as db:
            return bool(db.scalar(select(func.count()).select_from(Store).where(Store.retention_days > 0)))

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self._stop.set()

    def next_run(self) -> str:
        at = parse_clock(get_settings().retention_cleanup_time)
        nxt = last_due(datetime.now(), at) + timedelta(days=1)
        return nxt.strftime("%Y-%m-%d %H:%M")

    def due(self) -> bool:
        due_local = last_due(datetime.now(), parse_clock(get_settings().retention_cleanup_time))
        due_utc = due_local.astimezone(timezone.utc).replace(tzinfo=None)
        with session_scope() as db:
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(
                AuditEvent.action.in_(RUN_ACTIONS), AuditEvent.actor == SCHEDULER_ACTOR))
        return last is None or last < due_utc

    def _loop(self) -> None:
        if self._stop.wait(self.STARTUP_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            try:
                if self.due():
                    self.last_result = run(actor=SCHEDULER_ACTOR)
            except Exception:
                log.exception("聊天记录定时清理异常")
            self._stop.wait(self.CHECK_SECONDS)


scheduler = RetentionScheduler()
