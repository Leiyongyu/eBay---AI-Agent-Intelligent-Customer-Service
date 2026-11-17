"""给买家的附件公网地址 /webhooks/media/<签名>/<文件名>：eBay 发消息带的妥投照片等从这里取（不需要登录，签名防猜）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import AftersalesFile, AsFileStatus
from ..sending.media_links import PATH, read

router = APIRouter(prefix=PATH, tags=["public-media"])


@router.api_route("/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def media(token: str, name: str, db: Session = Depends(get_db)):
    fid = read(token)
    f = db.get(AftersalesFile, fid) if fid else None
    if f is None or f.status != AsFileStatus.STORED or not f.rel_path:
        raise HTTPException(404, "not found")
    path = get_settings().aftersales_root / f.rel_path
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type=f.content_type or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400", "X-Robots-Tag": "noindex"})
