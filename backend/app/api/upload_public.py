"""买家上传页的对外接口（不需要登录，只开放 /upload 这一组；隧道上 upload 子域名也只放行 /upload 开头的路径）。

文件本身不经过这里：浏览器拿 /files/{id}/parts 签发的地址把分块直接传到 Cloudflare R2。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..upload_page import guard
from ..upload_page.service import (
    UploadError,
    complete_file,
    ready,
    register_file,
    remove_file,
    save_form,
    session_upload,
    sign_parts,
    submit,
    verify,
)

router = APIRouter(prefix="/upload", tags=["upload-page"])
PAGE = Path(__file__).resolve().parent.parent / "upload_page" / "page" / "index.html"
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
           "X-Content-Type-Options": "nosniff"}


def _err(e: UploadError):
    return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.status_code, headers=HEADERS)


def _ready():
    if not ready():
        raise HTTPException(503, {"detail": "上传页暂未开放", "code": "closed"})


@router.get("", include_in_schema=False)
def page_redirect(request: Request):
    return RedirectResponse(str(request.url).replace("/upload", "/upload/", 1))


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
def page():
    s = get_settings()
    html = PAGE.read_text(encoding="utf-8").replace("{{TURNSTILE_SITE_KEY}}", s.turnstile_site_key or "") \
        .replace("{{OPEN}}", "true" if ready() else "false")
    return HTMLResponse(html, headers=HEADERS)


class VerifyBody(BaseModel):
    order_id: str | None = Field(default=None, max_length=32)
    zip: str | None = Field(default=None, max_length=32)
    username: str | None = Field(default=None, max_length=64)
    link_token: str | None = Field(default=None, max_length=200)
    turnstile_token: str | None = Field(default=None, max_length=4096)
    language: str | None = Field(default=None, pattern="^(de|en)$")


@router.post("/api/verify")
def api_verify(body: VerifyBody, request: Request, db: Session = Depends(get_db)):
    _ready()
    ip = guard.client_ip(request)
    if not guard.limiter.hit(f"verify-ip:{ip}", 20, 3600):
        return _err(UploadError(429, "尝试次数太多，请一小时后再试", "rate"))
    if not body.link_token and not guard.turnstile_ok(body.turnstile_token, ip):
        return _err(UploadError(403, "人机验证没通过，请刷新页面重试", "captcha"))
    try:
        return JSONResponse(verify(db, order_id=body.order_id, zip_code=body.zip, username=body.username,
                                   link_token=body.link_token, language=body.language, ip=ip), headers=HEADERS)
    except UploadError as e:
        db.rollback()
        return _err(e)


def _session(db: Session, token: str | None):
    _ready()
    return session_upload(db, token)


@router.get("/api/state")
def api_state(db: Session = Depends(get_db), x_upload_session: str | None = Header(default=None)):
    """刷新页面后接着传：返回本次上传的表单和文件状态。"""
    try:
        up = _session(db, x_upload_session)
        from ..upload_page.service import _order_items, _public, fetch_order

        return _public(db, up, x_upload_session, _order_items(fetch_order(up.store_id, up.order_id) or {}))
    except UploadError as e:
        return _err(e)


class FormBody(BaseModel):
    item_id: str | None = Field(default=None, max_length=64)
    item_title: str | None = Field(default=None, max_length=300)
    issue_type: str = Field(max_length=32)
    description: str | None = Field(default=None, max_length=4000)
    vehicle: dict | None = None
    installed: str | None = Field(default=None, max_length=16)


@router.post("/api/form")
def api_form(body: FormBody, db: Session = Depends(get_db), x_upload_session: str | None = Header(default=None)):
    try:
        save_form(db, _session(db, x_upload_session), body.model_dump())
        return {"ok": True}
    except UploadError as e:
        return _err(e)


class FileBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    type: str | None = Field(default=None, max_length=100)


@router.post("/api/files")
def api_register(body: FileBody, db: Session = Depends(get_db), x_upload_session: str | None = Header(default=None)):
    try:
        return register_file(db, _session(db, x_upload_session), name=body.name, size=body.size, content_type=body.type)
    except UploadError as e:
        db.rollback()
        return _err(e)


class PartsBody(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=50)


@router.post("/api/files/{file_id}/parts")
def api_parts(file_id: int, body: PartsBody, db: Session = Depends(get_db),
              x_upload_session: str | None = Header(default=None)):
    try:
        return sign_parts(db, _session(db, x_upload_session), file_id, body.part_numbers)
    except UploadError as e:
        return _err(e)


class CompleteBody(BaseModel):
    parts: list[dict] = Field(min_length=1, max_length=10000)


@router.post("/api/files/{file_id}/complete")
def api_complete(file_id: int, body: CompleteBody, db: Session = Depends(get_db),
                 x_upload_session: str | None = Header(default=None)):
    try:
        return complete_file(db, _session(db, x_upload_session), file_id, body.parts)
    except UploadError as e:
        db.commit()  # 大小不一致时文件已标为丢弃
        return _err(e)


@router.delete("/api/files/{file_id}")
def api_remove(file_id: int, db: Session = Depends(get_db), x_upload_session: str | None = Header(default=None)):
    try:
        remove_file(db, _session(db, x_upload_session), file_id)
        return {"ok": True}
    except UploadError as e:
        return _err(e)


@router.post("/api/submit")
def api_submit(db: Session = Depends(get_db), x_upload_session: str | None = Header(default=None)):
    try:
        out = submit(db, _session(db, x_upload_session))
    except UploadError as e:
        return _err(e)
    db.commit()
    from ..aftersales_mail.service import worker

    if worker.running:
        worker.trigger()  # 马上去 R2 拉回（后台任务在上传页配好后随程序启动）
    return out
