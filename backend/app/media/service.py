"""附件处理（第 20 章）。

安全约束：
- 只下载 eBay messageMedia 给出的地址：必须 HTTPS、域名在 MEDIA_ALLOWED_HOSTS 白名单内；
  重定向逐跳检查，不跟随到白名单之外；买家正文里贴的链接一律不打开。
- 下载时边读边计数，超过 MEDIA_MAX_BYTES 立即中止；不发送 eBay 令牌（附件地址无需授权，避免令牌外泄）。
- 只处理图片：用 Pillow 解码校验真实格式（不信扩展名和 Content-Type），重新编码为 JPEG/PNG，
  丢弃 EXIF（可能含 GPS 位置）等全部元数据，最长边缩到 MEDIA_MAX_DIMENSION。
- 文件按保留期删除；PDF、DOC 等非图片附件不下载，提示审核员到 eBay 后台查看。
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import utcnow
from ..models import AttachmentStatus, Message, MessageAttachment

log = logging.getLogger(__name__)

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP"}
Image.MAX_IMAGE_PIXELS = 60_000_000  # 防解压炸弹：超过即拒绝


class MediaError(Exception):
    """附件不能处理的原因（写入 status_reason，给审核员看）。"""


# ---------------------------------------------------------------- 登记
def register_ebay_media(db: Session, msg: Message, media: list[dict] | None) -> list[MessageAttachment]:
    """把 eBay messageMedia 登记为附件；只登记不下载（下载在 Agent 运行时按需进行）。"""
    s = get_settings()
    created = []
    for i, m in enumerate(media or [], 1):
        media_type = (m.get("mediaType") or "").upper() or "UNKNOWN"
        att = MessageAttachment(store_id=msg.store_id, conversation_id=msg.conversation_id, message_id=msg.id, seq=i,
                                media_name=(m.get("mediaName") or "")[:300] or None, media_type=media_type,
                                source="ebay", source_url=(m.get("mediaUrl") or "")[:1000] or None)
        if i > s.media_max_per_message:
            _skip(att, f"超过每条消息最多 {s.media_max_per_message} 个附件的上限")
        elif media_type != "IMAGE":
            _skip(att, f"{media_type} 类附件不自动下载，请到 eBay 后台查看")
        elif not att.source_url:
            _skip(att, "eBay 未提供附件地址")
        db.add(att)
        created.append(att)
    db.flush()
    return created


def add_uploaded_image(db: Session, msg: Message, seq: int, name: str | None, data: bytes) -> MessageAttachment:
    """客户测试消息系统（或测试）上传的图片：直接校验并保存（与 eBay 下载后的处理完全相同）。"""
    att = MessageAttachment(store_id=msg.store_id, conversation_id=msg.conversation_id, message_id=msg.id, seq=seq,
                            media_name=(name or "")[:300] or None, media_type="IMAGE", source="simulator")
    db.add(att)
    db.flush()
    try:
        store_image(att, data)
    except MediaError as e:
        _fail(att, str(e))
    db.flush()
    return att


def _skip(att: MessageAttachment, reason: str) -> None:
    att.status, att.status_reason = AttachmentStatus.SKIPPED, reason[:300]


def _fail(att: MessageAttachment, reason: str) -> None:
    att.status, att.status_reason = AttachmentStatus.FAILED, reason[:300]


# ---------------------------------------------------------------- 下载
def host_allowed(url: str, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    return any(host == h or host.endswith("." + h) for h in s.media_allowed_host_list)


def download(url: str, client: httpx.Client | None = None, settings: Settings | None = None) -> bytes:
    s = settings or get_settings()
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        for _ in range(3):  # 最多跟随 2 次重定向，每一跳都检查域名
            if not host_allowed(url, s):
                raise MediaError(f"附件地址不是 HTTPS 或域名不在白名单内：{urlparse(url).hostname or url[:60]}")
            with client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise MediaError(f"下载失败：HTTP {resp.status_code}")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > s.media_max_bytes:
                    raise MediaError(f"附件超过 {s.media_max_bytes // 1_000_000}MB 上限")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > s.media_max_bytes:
                        raise MediaError(f"附件超过 {s.media_max_bytes // 1_000_000}MB 上限")
                return bytes(buf)
        raise MediaError("重定向次数过多")
    except httpx.HTTPError as e:
        raise MediaError(f"下载失败：{type(e).__name__}") from None
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------- 校验与保存
def sanitize_image(data: bytes, settings: Settings | None = None) -> tuple[bytes, str, int, int]:
    """解码校验 → 按 EXIF 方向摆正 → 缩放 → 重新编码（不带任何元数据）。返回 (字节, MIME, 宽, 高)。"""
    s = settings or get_settings()
    if len(data) > s.media_max_bytes:
        raise MediaError(f"附件超过 {s.media_max_bytes // 1_000_000}MB 上限")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()  # 校验文件结构；verify 后对象不可再用
        if fmt not in ALLOWED_FORMATS:
            raise MediaError(f"不支持的图片格式：{fmt}")
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)  # GIF 只取第一帧
            img = ImageOps.exif_transpose(img)
            img.thumbnail((s.media_max_dimension, s.media_max_dimension))
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            out = io.BytesIO()
            if has_alpha:
                img.convert("RGBA").save(out, "PNG", optimize=True)
                mime = "image/png"
            else:
                img.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
                mime = "image/jpeg"
            return out.getvalue(), mime, img.width, img.height
    except MediaError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as e:
        raise MediaError(f"不是可识别的图片：{type(e).__name__}") from None


def store_image(att: MessageAttachment, data: bytes, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    clean, mime, width, height = sanitize_image(data, s)
    digest = hashlib.sha256(clean).hexdigest()
    rel = Path(att.store_id) / utcnow().strftime("%Y%m") / f"{digest}{'.png' if mime == 'image/png' else '.jpg'}"
    target = s.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(clean)
        tmp.replace(target)
    att.status, att.status_reason = AttachmentStatus.STORED, None
    att.content_type, att.size_bytes, att.width, att.height = mime, len(clean), width, height
    att.sha256, att.local_path = digest, rel.as_posix()
    att.expires_at = utcnow() + timedelta(days=s.media_retention_days)


def ensure_stored(db: Session, atts: list[MessageAttachment], client: httpx.Client | None = None) -> None:
    """把待下载的 eBay 图片下载并保存；失败的标 FAILED 并写原因，不抛异常。"""
    for att in atts:
        if att.status != AttachmentStatus.PENDING:
            continue
        try:
            store_image(att, download(att.source_url or "", client))
        except MediaError as e:
            _fail(att, str(e))
        except Exception as e:  # 单个附件出错不影响整个回复任务
            log.exception("附件 %s 处理异常", att.id)
            _fail(att, f"处理异常：{type(e).__name__}")
    db.flush()


def file_path(att: MessageAttachment) -> Path | None:
    if att.status != AttachmentStatus.STORED or not att.local_path:
        return None
    root = get_settings().media_root.resolve()
    path = (root / att.local_path).resolve()
    if root not in path.parents or not path.exists():  # 防路径穿越
        return None
    return path


# ---------------------------------------------------------------- 查询与清理
def attachments_for_messages(db: Session, message_ids: list[int]) -> list[MessageAttachment]:
    if not message_ids:
        return []
    return list(db.scalars(select(MessageAttachment).where(MessageAttachment.message_id.in_(message_ids))
                           .order_by(MessageAttachment.message_id, MessageAttachment.seq)))


def cleanup_expired(db: Session) -> int:
    """删除过了保留期的文件；同一图片（同哈希）被未过期附件引用时保留文件。"""
    now = utcnow()
    expired = db.scalars(select(MessageAttachment).where(MessageAttachment.status == AttachmentStatus.STORED,
                                                         MessageAttachment.expires_at < now)).all()
    for att in expired:
        still_used = db.scalar(select(MessageAttachment.id).where(
            MessageAttachment.local_path == att.local_path, MessageAttachment.status == AttachmentStatus.STORED,
            MessageAttachment.expires_at >= now, MessageAttachment.id != att.id).limit(1))
        path = file_path(att)
        if path and not still_used:
            path.unlink(missing_ok=True)
        att.status, att.status_reason, att.local_path = AttachmentStatus.EXPIRED, "超过保留期，文件已删除", None
    db.flush()
    return len(expired)
