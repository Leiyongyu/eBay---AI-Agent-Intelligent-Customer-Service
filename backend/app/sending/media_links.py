"""发给买家的附件（谷仓妥投照片、签收单等）的公网链接：eBay sendMessage 的 messageMedia 只接受 HTTPS 地址。

- 链接经 Cloudflare 隧道（PUBLIC_MEDIA_BASE_URL，默认取 eBay 推送地址的域名，如 https://hook.diwhx.xyz）访问本机，
  路径 /webhooks/media/<签名>/<文件名>，签名里带文件 ID 和过期时间，猜不到、改不了；
- 有效期跟谷仓附件保留天数（LOGISTICS_POD_RETENTION_DAYS）一致，文件清理后链接自然失效；
- 只放得出状态正常（STORED）的售后附件文件，不需要登录。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from ..config import get_settings
from ..db import utcnow

PATH = "/webhooks/media"


def _key() -> bytes:
    s = get_settings()
    base = s.upload_link_secret or s.credentials_master_key
    if not base:
        raise RuntimeError("UPLOAD_LINK_SECRET 和 CREDENTIALS_MASTER_KEY 都没配置，不能生成附件公网链接")
    return hmac.new(base.encode(), b"media-link", hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def public_base() -> str | None:
    """附件公网地址的前缀（HTTPS）；没配置返回 None。"""
    s = get_settings()
    base = (s.public_media_base_url or "").strip().rstrip("/")
    if not base and s.ebay_notify_endpoint:
        u = urlsplit(s.ebay_notify_endpoint)
        base = f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""
    return base if base.startswith("https://") else None


def sign(file_id: int, days: int | None = None) -> str:
    days = days or max(30, get_settings().logistics_pod_retention_days)
    exp = int((utcnow() + timedelta(days=days)).replace(tzinfo=timezone.utc).timestamp())
    payload = f"{file_id}.{exp}"
    return f"{payload}.{_b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest()[:18])}"


def read(token: str) -> int | None:
    """签名对且没过期返回文件 ID，否则 None。"""
    try:
        fid, exp, sig = token.split(".")
        good = _b64(hmac.new(_key(), f"{fid}.{exp}".encode(), hashlib.sha256).digest()[:18])
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return int(fid)
    except (ValueError, RuntimeError):
        return None


def url_for(file_id: int, name: str | None) -> str | None:
    base = public_base()
    if not base:
        return None
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file")[:80] or "file"
    return f"{base}{PATH}/{sign(file_id)}/{quote(safe)}"


def expires_text(days: int | None = None) -> str:
    days = days or max(30, get_settings().logistics_pod_retention_days)
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
