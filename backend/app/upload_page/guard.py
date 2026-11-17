"""上传页的防护：签名链接、Turnstile 人机验证、限流、IP 脱敏。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from datetime import datetime, timezone

import httpx

from ..config import get_settings

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_turnstile_override = None


# ------------------------------------------------------------------ 签名链接（客服生成，带过期时间）
def _key() -> bytes:
    s = get_settings()
    if s.upload_link_secret:
        return s.upload_link_secret.encode()
    if not s.credentials_master_key:
        raise RuntimeError("UPLOAD_LINK_SECRET 和 CREDENTIALS_MASTER_KEY 都没配置，不能签发上传链接")
    return hmac.new(s.credentials_master_key.encode(), b"upload-link", hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def sign_link(upload_id: int, expires_at: datetime) -> str:
    payload = f"{upload_id}.{int(expires_at.replace(tzinfo=timezone.utc).timestamp())}"
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).digest()[:18]
    return f"{payload}.{_b64(sig)}"


def read_link(token: str) -> int | None:
    """签名对且没过期返回上传记录 ID，否则 None。"""
    try:
        uid, exp, sig = token.split(".")
        payload = f"{uid}.{exp}"
        good = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return int(uid)
    except (ValueError, RuntimeError):
        return None


# ------------------------------------------------------------------ Turnstile
def set_turnstile_override(fn) -> None:
    global _turnstile_override
    _turnstile_override = fn


def turnstile_ok(token: str | None, ip: str | None) -> bool:
    if _turnstile_override is not None:
        return _turnstile_override(token, ip)
    s = get_settings()
    if not token or not s.turnstile_secret_key:
        return False
    try:
        r = httpx.post(TURNSTILE_URL, data={"secret": s.turnstile_secret_key, "response": token,
                                            **({"remoteip": ip} if ip else {})}, timeout=10)
        return bool(r.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False


# ------------------------------------------------------------------ 限流（有 Redis 用 Redis，没有用进程内计数）
class Limiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._mem: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, limit: int, window: int) -> bool:
        """计一次；超过 limit 返回 False。"""
        from ..redis_bus import bus

        c = bus.client()
        if c is not None:
            try:
                k = bus.key(f"upload:rl:{key}")
                n = c.incr(k)
                if n == 1:
                    c.expire(k, window)
                return n <= limit
            except Exception:  # noqa: BLE001 — Redis 不可用时退回进程内计数
                pass
        now = time.monotonic()
        with self._lock:
            start, n = self._mem.get(key, (now, 0))
            if now - start > window:
                start, n = now, 0
            n += 1
            self._mem[key] = (start, n)
            return n <= limit

    def reset(self) -> None:
        with self._lock:
            self._mem.clear()


limiter = Limiter()


def client_ip(request) -> str | None:
    """经 Cloudflare 隧道进来，真实 IP 在 CF-Connecting-IP。"""
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)


def mask_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + ":x"
    parts = ip.split(".")
    return ".".join(parts[:3] + ["x"]) if len(parts) == 4 else None
