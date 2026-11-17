"""参考文档库接口：列表、上传、改信息、替换新版本、归档 / 恢复、删除（被引用时拦住）、预览 / 下载、引用清单。

上传和替换的文件以 base64 放在 JSON 里（与知识库 Excel 上传同一做法，不引入 multipart）。
越权（不是自己负责店铺的文档）一律 404；敏感文档非管理员不能下载。
"""
from __future__ import annotations

import base64
import binascii
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import current, current_user, require_admin
from ..db import get_db
from ..knowledge import kb_docs as kd
from ..models import KbDocument, KbDocumentVersion, Store
from .deps import iso

router = APIRouter(prefix="/internal/kb-docs", tags=["kb-docs"])


def _err(e: kd.KbDocError):
    raise HTTPException(e.status, {"message": e.message, **(e.detail or {})} if e.detail else e.message) from None


def _load(db: Session, doc_id: int) -> KbDocument:
    doc = db.get(KbDocument, doc_id)
    if doc is None or not kd.can_see(current(), doc):
        raise HTTPException(404, "文档不存在或不在你负责的店铺")
    return doc


def _decode(data_base64: str) -> bytes:
    raw = data_base64.split(",", 1)[1] if data_base64.startswith("data:") else data_base64
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "文件内容不是有效的 base64") from None


def _dict(db: Session, d: KbDocument, refs: dict[str, int], names: dict[str, str]) -> dict:
    user = current()
    full = kd.can_download(user, d)
    out = {"id": d.id, "doc_no": d.doc_no, "title": d.title, "category": d.category, "sensitive": d.sensitive,
           "status": d.status, "store_ids": d.store_ids or [], "stores": [names.get(s, s) for s in d.store_ids or []],
           "ref_count": refs.get(d.doc_no, 0), "can_download": full}
    if not full:  # 敏感文档对非管理员只给标题
        return out
    return {**out, "description": d.description, "version": d.version, "file_name": d.file_name,
            "content_type": d.content_type, "size": d.size, "scan_status": d.scan_status, "file_url": kd.file_url(d),
            "uploaded_by": d.uploaded_by, "uploaded_at": iso(d.uploaded_at), "updated_by": d.updated_by,
            "updated_at": iso(d.updated_at)}


@router.get("")
def list_docs(q: str | None = None, category: str | None = None, status: str = "ACTIVE", db: Session = Depends(get_db)):
    """status：ACTIVE 使用中（默认）/ ARCHIVED 已归档 / ALL 全部。按负责店铺过滤。"""
    query = select(KbDocument).order_by(KbDocument.doc_no)
    if status in ("ACTIVE", "ARCHIVED"):
        query = query.where(KbDocument.status == status)
    if category:
        query = query.where(KbDocument.category == category)
    if q and q.strip():
        like = f"%{q.strip().replace('%', '').replace('_', '')}%"
        query = query.where(or_(KbDocument.doc_no.ilike(like), KbDocument.title.ilike(like),
                                KbDocument.description.ilike(like), KbDocument.file_name.ilike(like)))
    user = current()
    refs = kd.ref_counts(db)
    names = dict(db.execute(select(Store.id, Store.name)).all())
    docs = [d for d in db.scalars(query) if kd.can_see(user, d)]
    return {"items": [_dict(db, d, refs, names) for d in docs], "categories": kd.CATEGORIES,
            "allowed_ext": sorted(x[1:] for x in kd.ALLOWED_EXT), "max_mb": kd.get_settings().kb_docs_max_mb}


class DocMeta(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    category: str = "其他"
    store_ids: list[str] | None = None
    sensitive: bool = False


class UploadBody(DocMeta):
    file_name: str = Field(min_length=1, max_length=255)
    data_base64: str


class FileBody(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    data_base64: str


@router.post("", dependencies=[Depends(require_admin)])
def upload(body: UploadBody, request: Request, db: Session = Depends(get_db)):
    try:
        doc = kd.create(db, body.model_dump(exclude={"file_name", "data_base64"}), body.file_name,
                        _decode(body.data_base64), current_user(request).display_name)
    except kd.KbDocError as e:
        _err(e)
    return _dict(db, doc, {}, dict(db.execute(select(Store.id, Store.name)).all()))


@router.put("/{doc_id}", dependencies=[Depends(require_admin)])
def update_meta(doc_id: int, body: DocMeta, request: Request, db: Session = Depends(get_db)):
    doc = _load(db, doc_id)
    try:
        kd.update(db, doc, body.model_dump(), current_user(request).display_name)
    except kd.KbDocError as e:
        _err(e)
    return _dict(db, doc, kd.ref_counts(db), dict(db.execute(select(Store.id, Store.name)).all()))


@router.post("/{doc_id}/replace", dependencies=[Depends(require_admin)])
def replace(doc_id: int, body: FileBody, request: Request, db: Session = Depends(get_db)):
    doc = _load(db, doc_id)
    try:
        kd.replace(db, doc, body.file_name, _decode(body.data_base64), current_user(request).display_name)
    except kd.KbDocError as e:
        _err(e)
    return _dict(db, doc, kd.ref_counts(db), dict(db.execute(select(Store.id, Store.name)).all()))


@router.post("/{doc_id}/archive", dependencies=[Depends(require_admin)])
def archive(doc_id: int, request: Request, db: Session = Depends(get_db)):
    kd.set_status(db, _load(db, doc_id), True, current_user(request).display_name)
    return {"ok": True}


@router.post("/{doc_id}/restore", dependencies=[Depends(require_admin)])
def restore(doc_id: int, request: Request, db: Session = Depends(get_db)):
    kd.set_status(db, _load(db, doc_id), False, current_user(request).display_name)
    return {"ok": True}


@router.delete("/{doc_id}", dependencies=[Depends(require_admin)])
def delete(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = _load(db, doc_id)
    try:
        return kd.delete(db, doc, current_user(request).display_name)
    except kd.KbDocError as e:
        _err(e)


@router.get("/{doc_id}/versions")
def versions(doc_id: int, db: Session = Depends(get_db)):
    doc = _load(db, doc_id)
    if not kd.can_download(current(), doc):
        return []
    rows = db.scalars(select(KbDocumentVersion).where(KbDocumentVersion.doc_id == doc.id)
                      .order_by(KbDocumentVersion.version.desc())).all()
    return [{"version": v.version, "file_name": v.file_name, "size": v.size, "uploaded_by": v.uploaded_by,
             "uploaded_at": iso(v.uploaded_at), "file_url": kd.file_url(doc, v.version)} for v in rows]


@router.get("/{doc_id}/refs")
def refs(doc_id: int, db: Session = Depends(get_db)):
    doc = _load(db, doc_id)
    names = dict(db.execute(select(Store.id, Store.name)).all())
    rows = kd.refs_of(db, [doc.doc_no]).get(doc.doc_no) or []
    user = current()
    if user is not None and not user.sees_all_stores:
        rows = [r for r in rows if r["store_id"] in user.store_ids]
    return [{**r, "store_name": names.get(r["store_id"], r["store_id"])} for r in rows]


@router.get("/{doc_id}/file")
def download(doc_id: int, request: Request, version: int | None = None, db: Session = Depends(get_db)):
    """pdf、图片、txt、md 在浏览器里直接打开，其他下载。越权 404；敏感文档非管理员 403。"""
    doc = _load(db, doc_id)
    if not kd.can_download(current(), doc):
        raise HTTPException(403, "这是敏感文档，只有管理员能查看")
    if version and version != doc.version:
        v = db.scalar(select(KbDocumentVersion).where(KbDocumentVersion.doc_id == doc.id, KbDocumentVersion.version == version))
        if v is None:
            raise HTTPException(404, "没有这个版本")
        path, name, ctype, ver = v.stored_path, v.file_name, v.content_type, v.version
    else:
        path, name, ctype, ver = doc.stored_path, doc.file_name, doc.content_type, doc.version
    f = kd.abs_path(path)
    if not f.is_file():
        raise HTTPException(404, "文件不在服务器上了")
    kd.log_download(db, doc, ver, current_user(request).display_name)
    inline = kd.ext_of(name) in kd.INLINE_EXT
    dispo = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"
    media = ctype or "application/octet-stream"
    if kd.ext_of(name) in (".txt", ".md", ".csv"):
        media = f"{media.split(';')[0]}; charset=utf-8"
    return FileResponse(f, media_type=media, headers={"Content-Disposition": dispo, "X-Content-Type-Options": "nosniff"})
