"""Redis 辅助通道（第 9.1 节）：目前只承担 Worker 唤醒信号与健康检查。

Redis 不存任何必须可靠的数据：任务、草稿、发送状态都在数据库。Redis 不可用时
自动退回进程内唤醒 + 轮询兜底，只在日志和状态页提示，不影响收消息和审核。
后续的限流、会话锁快速路径、eBay token 缓存也放在这里，统一使用 REDIS_KEY_PREFIX。
"""
from __future__ import annotations

import logging
import time

from .config import get_settings

log = logging.getLogger(__name__)


class RedisBus:
    def __init__(self):
        s = get_settings()
        self.url = s.redis_url
        self.prefix = s.redis_key_prefix
        self.block_seconds = max(1, int(round(s.worker_poll_seconds)))
        self._client = None
        self._down_until = 0.0  # 连接失败后暂停重试一段时间，避免每次都卡在超时上
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def client(self):
        if not self.configured or time.monotonic() < self._down_until:
            return None
        if self._client is None:
            import redis

            # protocol=2：redis-py 8 默认用 RESP3（HELLO 3），本机 Redis 5.0 不支持，会直接连接失败
            self._client = redis.Redis.from_url(
                self.url, socket_connect_timeout=2, socket_timeout=self.block_seconds + 3, decode_responses=True,
                protocol=2,
            )
        return self._client

    def _failed(self, e: Exception) -> None:
        if self.last_error is None:
            log.warning("Redis 不可用，退回进程内唤醒：%s", e)
        self.last_error = f"{type(e).__name__}: {e}"
        self._down_until = time.monotonic() + 30

    def _ok(self) -> None:
        if self.last_error is not None:
            log.info("Redis 已恢复")
        self.last_error = None

    def ping(self) -> dict:
        if not self.configured:
            return {"configured": False, "ok": False}
        self._down_until = 0.0  # 手动检查时立即重试
        try:
            c = self.client()
            c.ping()
            info = c.info("server")
            self._ok()
            return {"configured": True, "ok": True, "server_version": info.get("redis_version")}
        except Exception as e:
            self._failed(e)
            return {"configured": True, "ok": False, "error": self.last_error}

    def wake(self) -> bool:
        c = self.client()
        if c is None:
            return False
        try:
            pipe = c.pipeline()
            pipe.lpush(self.key("worker:wake"), "1")
            pipe.ltrim(self.key("worker:wake"), 0, 99)
            pipe.execute()
            self._ok()
            return True
        except Exception as e:
            self._failed(e)
            return False

    def wait(self) -> bool | None:
        """阻塞等待唤醒信号。返回 True=收到信号，False=超时，None=Redis 不可用（调用方改用本地等待）。"""
        c = self.client()
        if c is None:
            return None
        try:
            got = c.blpop([self.key("worker:wake")], timeout=self.block_seconds)
            if got:
                c.delete(self.key("worker:wake"))  # 合并积压的信号
            self._ok()
            return bool(got)
        except Exception as e:
            self._failed(e)
            return None


bus = RedisBus()
