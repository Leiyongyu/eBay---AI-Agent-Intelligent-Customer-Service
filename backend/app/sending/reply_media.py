"""工作台回复里带的图片：客服上传 → 存成售后附件（来源 REPLY，挂在会话上）→ 发送时随消息发给买家。

- 只收 PNG / JPG / GIF / WEBP，单张不超过 5 MB，一条回复最多 5 张（和查件结果附件同一套上限）；
- 对接了 eBay 的店铺：eBay 要一个公网 HTTPS 地址取图（sending/media_links 的签名链接），没有公网地址就发不了，
  发送前直接报错，不会只发文字；
- 本地模式的店铺：图片只记在本系统的消息上。
"""
from __future__ import annotations

import base64
import binascii

from sqlalchemy.orm import Session

from ..aftersales_mail import storage
from ..db import utcnow
from ..models import AftersalesFile, Conversation, Store
from .service import SendError

MAX_IMAGES = 5
MAX_BYTES = 5 * 1024 * 1024
_MAGIC = [(b"\x89PNG", ".png", "image/png"), (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
          (b"GIF8", ".gif", "image/gif"), (b"RIFF", ".webp", "image/webp")]


def save_image(db: Session, conv: Conversation, name: str | None, data_base64: str, actor: str) -> AftersalesFile:
    raw = data_base64.split(",", 1)[1] if data_base64.startswith("data:") else data_base64
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise SendError(422, "图片数据不对")
    if len(data) > MAX_BYTES:
        raise SendError(422, "图片不能超过 5 MB")
    hit = next(((ext, ct) for magic, ext, ct in _MAGIC if data.startswith(magic)), None)
    if hit is None or (hit[0] == ".webp" and data[8:12] != b"WEBP"):
        raise SendError(422, "只支持 PNG、JPG、GIF、WEBP 图片")
    ext, ctype = hit
    store = db.get(Store, conv.store_id)
    folder = storage.store_dir(conv.store_id, store.ebay_user_id if store else None)
    now = utcnow()
    saved = storage.save_stream([data], folder, f"回复图片_{conv.id}_{now:%Y%m%d-%H%M%S}{ext}", ctype)
    f = AftersalesFile(mail_id=None, store_id=conv.store_id, conversation_id=conv.id, source="REPLY",
                       original_name=((name or "").strip() or f"image{ext}")[:300], content_type=ctype,
                       size_bytes=saved.size, sha256=saved.sha256, rel_path=saved.rel_path, status="STORED",
                       status_reason=f"{actor} 在工作台回复里上传的图片", received_at=now)
    db.add(f)
    db.flush()
    return f


def build_meta(db: Session, conv: Conversation, file_ids: list[int]) -> dict | None:
    """发送时：把要带的图片转成发件箱 meta（media 给本系统显示，ebay_media 给 eBay）。不合格直接报错。"""
    ids = list(dict.fromkeys(file_ids or []))
    if not ids:
        return None
    if len(ids) > MAX_IMAGES:
        raise SendError(422, f"一条回复最多带 {MAX_IMAGES} 张图片")
    media = []
    for fid in ids:
        f = db.get(AftersalesFile, fid)
        if f is None or f.conversation_id != conv.id or f.status != "STORED" or not (f.content_type or "").startswith("image/"):
            raise SendError(422, f"图片 {fid} 不存在或不属于这个会话，请重新上传")
        media.append({"mediaName": f.original_name or "image", "mediaType": "IMAGE", "fileId": f.id})
    ebay_media = None
    from .channel import is_live_store

    if is_live_store(db.get(Store, conv.store_id)) and conv.channel_source == "ebay":
        from .media_links import url_for

        urls = [url_for(m["fileId"], m["mediaName"]) for m in media]
        if None in urls:
            raise SendError(422, "没有附件公网地址（PUBLIC_MEDIA_BASE_URL / EBAY_NOTIFY_ENDPOINT），图片发不到 eBay；"
                                 "请去掉图片，或配置好公网地址后再发")
        ebay_media = [{"mediaName": m["mediaName"], "mediaType": "IMAGE", "mediaUrl": u} for m, u in zip(media, urls)]
    return {"media": media, "ebay_media": ebay_media}
