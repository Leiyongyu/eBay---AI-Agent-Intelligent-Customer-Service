"""后台 Worker（第 9.2 节 worker 的进程内版本）。

本地开发时与 API 同进程跑一个线程：
- 认领合并窗口已结束的 RECEIVED 任务（乐观锁 lock_version，换 PostgreSQL 后可改 FOR UPDATE SKIP LOCKED 并多副本）
- 执行 Agent，失败按退避重试，超过上限转 FAILED_HANDOFF
- 执行 QUEUED 发送意图
入队时调用 wake() 立即唤醒，信号丢失时靠轮询兜底。
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import select, update

from .agent.runner import AgentRunner
from .config import get_settings
from .db import dialect_name, session_scope, utcnow
from .models import JobState, ReplyJob, SendIntent
from .redis_bus import bus
from .sending.service import dispatch

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class Worker:
    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.runner = AgentRunner()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.recover_stuck()
        self._thread = threading.Thread(target=self._loop, name="agent-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()
        bus.wake()  # 配了 Redis 时，其他进程里的 Worker 也能被唤醒

    def _loop(self) -> None:
        poll = get_settings().worker_poll_seconds
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:
                log.exception("Worker 循环异常")
                busy = False
            if busy:
                continue
            if self._wake.is_set():
                self._wake.clear()
                continue
            if bus.wait() is None:  # Redis 未配置或不可用：本地等待，轮询兜底
                self._wake.wait(poll)
            self._wake.clear()

    # ------------------------------------------------------------ 单次处理（测试可直接调用）
    def run_once(self) -> bool:
        sent = self.dispatch_sends()
        job_id = self.claim_next_job()
        if job_id is None:
            return sent > 0
        try:
            result = self.runner.run(job_id)
            log.info("任务 %s 完成：%s %s", job_id, result["decision"], result["reasons"])
        except Exception as e:
            log.exception("任务 %s 运行失败", job_id)
            self._mark_failed(job_id, e)
        return True

    def claim_next_job(self) -> int | None:
        with session_scope() as db:
            q = (
                select(ReplyJob)
                .where(ReplyJob.state.in_([JobState.RECEIVED, JobState.RETRYABLE_ERROR]), ReplyJob.ready_at <= utcnow())
                .order_by(ReplyJob.ready_at)
                .limit(1)
            )
            if dialect_name() == "postgresql":
                # 多个 Worker 并发时互不阻塞（第 9.1 节）；lock_version 乐观锁仍保留作为第二道保护
                q = q.with_for_update(skip_locked=True)
            job = db.scalar(q)
            if job is None:
                return None
            claimed = db.execute(
                update(ReplyJob)
                .where(ReplyJob.id == job.id, ReplyJob.lock_version == job.lock_version)
                .values(state=JobState.RUNNING, lock_version=job.lock_version + 1, attempt=job.attempt + 1,
                        updated_at=utcnow())
            ).rowcount
            return job.id if claimed == 1 else None

    def _mark_failed(self, job_id: int, error: Exception) -> None:
        with session_scope() as db:
            job = db.get(ReplyJob, job_id)
            if job.state != JobState.RUNNING:
                return
            job.last_error = f"{type(error).__name__}: {error}"[:2000]
            if job.attempt >= MAX_ATTEMPTS:
                job.state = JobState.FAILED_HANDOFF
            else:
                job.state = JobState.RETRYABLE_ERROR
                job.ready_at = utcnow() + timedelta(seconds=10 * 2 ** job.attempt)
            job.lock_version += 1

    def dispatch_sends(self) -> int:
        count = 0
        with session_scope() as db:
            for intent in db.scalars(select(SendIntent).where(SendIntent.state == "QUEUED").limit(20)).all():
                dispatch(db, intent)
                count += 1
        return count

    def recover_stuck(self) -> None:
        """进程重启：RUNNING 的生成任务从头重跑；SENDING 的意图无法确认是否已发出，转 SEND_UNCERTAIN。"""
        with session_scope() as db:
            for job in db.scalars(select(ReplyJob).where(ReplyJob.state == JobState.RUNNING)).all():
                job.state = JobState.RECEIVED
                job.lock_version += 1
            for intent in db.scalars(select(SendIntent).where(SendIntent.state == "SENDING")).all():
                intent.state = "SEND_UNCERTAIN"
                job = db.get(ReplyJob, intent.job_id)
                job.state = JobState.SEND_UNCERTAIN
                job.lock_version += 1


worker = Worker()
