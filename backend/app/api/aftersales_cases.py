"""飞书售后群工单：会话里的工单（起草、修改、发到群里、完结）、工单列表、马上读一次群回复。按负责店铺。"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import allowed_store_ids, check_store
from ..config import get_settings
from ..db import get_db
from ..feishu import cases as svc
from ..feishu.client import FeishuError
from ..models import AftersalesCase, AttachmentStatus, Conversation, MessageAttachment, Store
from .deps import iso, reviewer_name

router = APIRouter(prefix="/internal", tags=["aftersales-cases"])
STATUS_LABELS = {"DRAFT": "待发送", "SENT": "已发待回复", "REPLIED": "群已回复待审核", "ANSWERED": "已回复买家", "CLOSED": "已完结"}


def _dict(db: Session, c: AftersalesCase) -> dict:
    atts = [db.get(MessageAttachment, i) for i in c.attachment_ids or []]
    return {"id": c.id, "conversation_id": c.conversation_id, "store_id": c.store_id, "order_id": c.order_id,
            "buyer_key": c.buyer_key, "marketplace_id": c.marketplace_id, "case_type": c.case_type, "category": c.category,
            "sku": c.sku, "title": svc.title(c), "problem_zh": c.problem_zh, "fault_info": c.fault_info,
            "original_text": c.original_text, "missing": c.missing or [], "status": c.status,
            "status_label": STATUS_LABELS.get(c.status, c.status), "origin": c.origin,
            "attachments": [{"id": a.id, "file_url": f"/internal/attachments/{a.id}/file"} for a in atts
                            if a is not None and a.status == AttachmentStatus.STORED],
            "replies": [{**r, "files": [f"/internal/aftersales-files/{i}/content" for i in r.get("file_ids") or []]}
                        for r in c.replies or []],
            "draft_reply": c.draft_reply, "draft_reply_zh": c.draft_reply_zh, "drafted_at": iso(c.drafted_at),
            "sent_by": c.sent_by, "sent_at": iso(c.sent_at), "answered_at": iso(c.answered_at),
            "closed_by": c.closed_by, "closed_at": iso(c.closed_at), "created_at": iso(c.created_at)}


def _load(db: Session, case_id: int) -> AftersalesCase:
    c = db.get(AftersalesCase, case_id)
    if c is None:
        raise HTTPException(404, "工单不存在")
    check_store(c.store_id)
    return c


@router.get("/conversations/{conv_id}/aftersales-case")
def conversation_case(conv_id: int, db: Session = Depends(get_db)):
    """会话最近的工单（没完结的优先）；没有返回 null。附是否已配置飞书。"""
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    c = svc.open_case(db, conv.id) or db.scalar(select(AftersalesCase).where(AftersalesCase.conversation_id == conv.id)
                                                .order_by(AftersalesCase.id.desc()).limit(1))
    return {"case": _dict(db, c) if c else None, "feishu_ready": get_settings().feishu_ready}


class CreateBody(BaseModel):
    category: str = Field(default="质量问题", max_length=32)


@router.post("/conversations/{conv_id}/aftersales-case")
def create_case(conv_id: int, body: CreateBody, db: Session = Depends(get_db)):
    """客服手动起草工单（AI 没建议时用）：按会话的订单和最近买家消息整理。"""
    from ..sources import get_source_for_store

    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    order = None
    try:
        rows = get_source_for_store(conv.store_id).find_orders(conv.store_id, conv.buyer_key) or []
        order = next((r for r in rows if conv.item_id and r.get("item_id") == conv.item_id), rows[0] if rows else None)
    except Exception:  # noqa: BLE001  查不到订单：按售前起草
        order = None
    c = svc.build(db, conv, category=body.category, order=order, origin="MANUAL")
    return _dict(db, c)


class EditBody(BaseModel):
    problem_zh: str | None = Field(default=None, max_length=5000)
    fault_info: str | None = Field(default=None, max_length=1000)
    original_text: str | None = Field(default=None, max_length=10000)
    attachment_ids: list[int] | None = None
    draft_reply: str | None = Field(default=None, max_length=5000)


@router.patch("/aftersales-cases/{case_id}")
def edit_case(case_id: int, body: EditBody, db: Session = Depends(get_db)):
    c = _load(db, case_id)
    if c.status == "DRAFT":
        for k in ("problem_zh", "fault_info", "original_text"):
            if getattr(body, k) is not None:
                setattr(c, k, getattr(body, k).strip())
        if body.attachment_ids is not None:
            c.attachment_ids = [i for i in body.attachment_ids if i in (c.attachment_ids or [])]
    elif any(getattr(body, k) is not None for k in ("problem_zh", "fault_info", "original_text", "attachment_ids")):
        raise HTTPException(409, "工单已经发出，不能再改工单内容")
    if body.draft_reply is not None:
        c.draft_reply = body.draft_reply.strip()
    return _dict(db, c)


@router.post("/aftersales-cases/{case_id}/send")
def send_case(case_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """发到飞书售后群：卡片 + 话题里的买家图片。"""
    c = _load(db, case_id)
    try:
        svc.send(db, c, reviewer)
    except svc.CaseError as e:
        raise HTTPException(409, str(e)) from None
    except FeishuError as e:
        raise HTTPException(502, str(e)) from None
    return _dict(db, c)


@router.post("/aftersales-cases/{case_id}/redraft")
def redraft(case_id: int, db: Session = Depends(get_db)):
    """按群里的回复重新起草买家回复。"""
    c = _load(db, case_id)
    if not c.replies:
        raise HTTPException(409, "群里还没有回复")
    svc.draft_reply(db, c)
    return _dict(db, c)


@router.post("/aftersales-cases/{case_id}/close")
def close_case(case_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    c = _load(db, case_id)
    try:
        svc.close(db, c, reviewer)
    except svc.CaseError as e:
        raise HTTPException(409, str(e)) from None
    return _dict(db, c)


@router.get("/aftersales-cases")
def list_cases(status: str | None = None, store_id: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    allowed = allowed_store_ids()
    base = select(AftersalesCase)
    if allowed is not None:
        base = base.where(AftersalesCase.store_id.in_(allowed))
    if store_id:
        base = base.where(AftersalesCase.store_id == store_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(AftersalesCase.order_id.ilike(like), AftersalesCase.buyer_key.ilike(like),
                              AftersalesCase.sku.ilike(like)))
    counts = {st: db.scalar(select(func.count()).select_from(base.where(AftersalesCase.status == st).subquery())) or 0
              for st in STATUS_LABELS}
    rows = db.scalars((base.where(AftersalesCase.status == status) if status else base)
                      .order_by(AftersalesCase.updated_at.desc()).limit(300)).all()
    names = dict(db.execute(select(Store.id, Store.name)).all())
    return {"counts": counts, "items": [{**_dict(db, c), "store_name": names.get(c.store_id)} for c in rows],
            "feishu_ready": get_settings().feishu_ready, "last_poll": svc.worker.last, "last_poll_at": iso(svc.worker.last_at)}


_polling = threading.Event()


@router.post("/aftersales-cases/poll")
def poll_now():
    """马上读一次群里的回复（后台进行，几秒后刷新）。"""
    if not get_settings().feishu_ready:
        raise HTTPException(409, "飞书售后群没有配置好")
    if _polling.is_set():
        return {"started": False}
    _polling.set()

    def run():
        try:
            svc.worker.run_now()
        finally:
            _polling.clear()

    threading.Thread(target=run, name="feishu-cases-now", daemon=True).start()
    return {"started": True}
