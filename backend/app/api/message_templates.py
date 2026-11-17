"""消息模板接口：列表（按类型页签、用途、标题/内容、创建人、标签、星标筛选）、新建、修改、另存为、删除、星标、
标签管理（改名 / 删除）、图片附件上传与读取。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import message_templates as mt
from ..auth import current_user
from ..db import get_db, utcnow
from ..models import AuditEvent, MessageTemplate
from .deps import iso

router = APIRouter(prefix="/internal/message-templates", tags=["message-templates"])
SCOPE_VIEW = {"system": "SYSTEM", "company": "COMPANY", "mine": "PERSONAL"}


class Attachment(BaseModel):
    file: str = Field(max_length=64)
    name: str | None = Field(default=None, max_length=200)


class TemplateBody(BaseModel):
    scope: str = "PERSONAL"
    usage: str = "MESSAGE"
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=mt.MAX_CONTENT)
    tags: list[str] = Field(default_factory=list, max_length=20)
    attachments: list[Attachment] = Field(default_factory=list, max_length=mt.MAX_ATTACHMENTS)


class StarBody(BaseModel):
    starred: bool


class UploadBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    data: str = Field(min_length=10, max_length=8 * 1024 * 1024)  # base64（含 data URL 前缀也行）


class TagRename(BaseModel):
    old: str = Field(min_length=1, max_length=32)
    new: str = Field(min_length=1, max_length=32)


class TagDelete(BaseModel):
    name: str = Field(min_length=1, max_length=32)


def _dict(t: MessageTemplate, user) -> dict:
    return {"id": t.id, "scope": t.scope, "usage": t.usage, "title": t.title, "content": t.content,
            "tags": t.tags or [], "attachments": [{**a, "url": f"/internal/message-templates/files/{a['file']}"}
                                                 for a in (t.attachments or [])],
            "starred": bool(t.starred), "created_by": t.created_by, "updated_by": t.updated_by,
            "created_at": iso(t.created_at), "updated_at": iso(t.updated_at),
            "mine": t.owner_id == user.id, "editable": mt.can_edit(t, user)}


def _visible(db: Session, user) -> list[MessageTemplate]:
    rows = db.scalars(select(MessageTemplate).order_by(MessageTemplate.scope, MessageTemplate.id)).all()
    return [t for t in rows if mt.can_see(t, user)]


def _clean(body: TemplateBody, user) -> dict:
    if body.usage not in mt.USAGES:
        raise HTTPException(422, "用途不对")
    if body.scope not in mt.SCOPES:
        raise HTTPException(422, "类型不对")
    if body.scope == "SYSTEM" and user.role != "ADMIN":
        raise HTTPException(403, "系统预定义模板只有管理员能改")
    for a in body.attachments:
        if mt.file_path(a.file) is None:
            raise HTTPException(422, f"附件 {a.name or a.file} 不存在，请重新上传")
    tags = list(dict.fromkeys(t.strip()[:32] for t in body.tags if t.strip()))
    return {"scope": body.scope, "usage": body.usage, "title": body.title.strip(), "content": body.content.strip(),
            "tags": tags or None, "attachments": [a.model_dump() for a in body.attachments] or None}


def _load(db: Session, tid: int, user, edit: bool = False) -> MessageTemplate:
    t = db.get(MessageTemplate, tid)
    if t is None or not mt.can_see(t, user):
        raise HTTPException(404, "模板不存在")
    if edit and not mt.can_edit(t, user):
        raise HTTPException(403, "这个模板你不能改（系统预定义只有管理员能改，企业模板只有创建人或管理员能改）；可以“另存为”自己的模板")
    return t


@router.get("")
def list_templates(request: Request, scope: str = "all", usage: str | None = None, q: str | None = None,
                   q_field: str = "title", creator: str | None = None, tag: str | None = None, starred: bool = False,
                   db: Session = Depends(get_db)):
    """scope：all 全部 / system 系统预定义 / company 企业模板 / mine 我的模板（我建的个人模板）。返回列表和各页签数量。"""
    user = current_user(request)
    rows = _visible(db, user)

    def match(t: MessageTemplate) -> bool:
        if usage and t.usage != usage:
            return False
        if q and q.strip():
            text = (t.content if q_field == "content" else t.title if q_field == "title" else f"{t.title}\n{t.content}")
            if q.strip().lower() not in text.lower():
                return False
        if creator and t.created_by != creator:
            return False
        if tag and tag not in (t.tags or []):
            return False
        return not starred or bool(t.starred)

    filtered = [t for t in rows if match(t)]
    counts = {"all": len(filtered), **{k: sum(1 for t in filtered if t.scope == v and (k != "mine" or t.owner_id == user.id))
                                       for k, v in SCOPE_VIEW.items()}}
    if scope in SCOPE_VIEW:
        filtered = [t for t in filtered if t.scope == SCOPE_VIEW[scope] and (scope != "mine" or t.owner_id == user.id)]
    return {"items": [_dict(t, user) for t in filtered], "counts": counts}


@router.get("/meta")
def meta(request: Request, db: Session = Depends(get_db)):
    """编辑页需要的：占位符说明、已有的标签、创建人（筛选下拉用）、当前账号能否维护系统预定义模板。"""
    user = current_user(request)
    rows = _visible(db, user)
    return {"placeholders": [{"label": a, "key": b} for a, b in mt.PLACEHOLDERS],
            "tags": sorted({x for t in rows for x in (t.tags or [])}),
            "creators": sorted({t.created_by for t in rows if t.created_by}),
            "can_edit_system": user.role == "ADMIN", "max_content": mt.MAX_CONTENT, "max_attachments": mt.MAX_ATTACHMENTS}


@router.post("")
def create(body: TemplateBody, request: Request, db: Session = Depends(get_db)):
    """新建（编辑页的“另存为”也用这个）。"""
    user = current_user(request)
    t = MessageTemplate(**_clean(body, user), owner_id=user.id, created_by=user.display_name[:64],
                        updated_by=user.display_name[:64])
    db.add(t)
    db.flush()
    db.add(AuditEvent(actor=user.display_name[:64], action="MESSAGE_TEMPLATE_CREATED", detail={"id": t.id, "title": t.title}))
    db.commit()
    return _dict(t, user)


@router.put("/{tid}")
def update(tid: int, body: TemplateBody, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    t = _load(db, tid, user, edit=True)
    data = _clean(body, user)
    if t.scope == "SYSTEM" and data["scope"] != "SYSTEM":
        raise HTTPException(422, "系统预定义模板不能改成别的类型；需要的话用“另存为”")
    for k, v in data.items():
        setattr(t, k, v)
    t.updated_by, t.updated_at = user.display_name[:64], utcnow()
    db.add(AuditEvent(actor=user.display_name[:64], action="MESSAGE_TEMPLATE_UPDATED", detail={"id": t.id, "title": t.title}))
    db.commit()
    return _dict(t, user)


@router.delete("/{tid}")
def delete(tid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    t = _load(db, tid, user, edit=True)
    db.add(AuditEvent(actor=user.display_name[:64], action="MESSAGE_TEMPLATE_DELETED", detail={"id": t.id, "title": t.title}))
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.put("/{tid}/star")
def star(tid: int, body: StarBody, request: Request, db: Session = Depends(get_db)):
    t = _load(db, tid, current_user(request))
    t.starred = body.starred
    db.commit()
    return {"starred": t.starred}


# ------------------------------------------------------------------ 标签管理
@router.post("/tags/rename")
def rename_tag(body: TagRename, request: Request, db: Session = Depends(get_db)):
    """把我能改的模板上的标签 old 改名为 new。"""
    user = current_user(request)
    n = 0
    for t in _visible(db, user):
        if mt.can_edit(t, user) and body.old in (t.tags or []):
            t.tags = list(dict.fromkeys(body.new.strip() if x == body.old else x for x in t.tags))
            n += 1
    db.commit()
    return {"updated": n}


@router.post("/tags/delete")
def delete_tag(body: TagDelete, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    n = 0
    for t in _visible(db, user):
        if mt.can_edit(t, user) and body.name in (t.tags or []):
            t.tags = [x for x in t.tags if x != body.name] or None
            n += 1
    db.commit()
    return {"updated": n}


# ------------------------------------------------------------------ 图片附件
@router.post("/files")
def upload(body: UploadBody):
    try:
        name = mt.save_image(body.data)
    except mt.TemplateFileError as e:
        raise HTTPException(422, str(e))
    return {"file": name, "name": body.name or name, "url": f"/internal/message-templates/files/{name}"}


@router.get("/files/{name}")
def get_file(name: str):
    p = mt.file_path(name)
    if p is None:
        raise HTTPException(404, "图片不存在")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------------ 在回复里使用
class RenderBody(BaseModel):
    conversation_id: int


@router.post("/{tid}/render")
def render(tid: int, body: RenderBody, request: Request, db: Session = Depends(get_db)):
    """把模板里的 {{buyer}}、{{orderNumber}} 等换成这个会话的实际内容（买家、店铺、商品、订单、运单）。
    取不到的占位符原样留着，在 missing 里列出来，客服发送前自己补。"""
    from ..auth import check_store
    from ..models import Conversation

    user = current_user(request)
    t = _load(db, tid, user)
    conv = db.get(Conversation, body.conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    text, missing = mt.render(db, t.content, conv)
    labels = dict((k, a) for a, k in mt.PLACEHOLDERS)
    return {"text": text, "missing": [{"key": k, "label": labels.get(k, k)} for k in missing],
            "attachments": _dict(t, user)["attachments"]}
