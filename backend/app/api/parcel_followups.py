"""异常包裹跟进表：断更、无轨迹、妥投未收到、退回、损坏、发错等包裹的处理记录（替代原 Excel《异常包裹跟进表》）。

- 每人只看、只改自己记的；管理员看全部（可按负责人筛选）、也能改；
- 系统自动登记：物流规则、谷仓查件单、eBay 纠纷 / 退货 / 退款出现时自动建或补写，写好“下一步”（见 parcel_auto.py）；
- 可关联买家会话：从会话右侧“异常跟进”新建时自动带上店铺、买家、订单、SKU、跟踪号、查件单号，列表里点“打开会话”直接跳过去；
- 只在本系统记录，不写回 eBay、谷仓。
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import CurrentUser, check_store, current_user
from ..db import get_db, utcnow
from ..models import AuditEvent, Conversation, EbayOrderLine, LogisticsTicket, ParcelFollowup, ShipNotice, Store
from .deps import iso

router = APIRouter(prefix="/internal/parcel-followups", tags=["parcel-followups"])

REASONS = ("断更", "无轨迹", "妥投未收到", "未收到货纠纷", "物流异常退回", "到货损坏", "谷仓发错", "供应商发错", "产品问题",
           "地址问题", "其他")
ACTIONS = ("重发", "退款", "部分退款", "补偿", "索赔", "等待查件", "等待买家", "其他")
STATUSES = ("OPEN", "DONE")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 谷仓查件类型 → 原因类型（从会话新建时预填）
TICKET_REASON = {"断更": "断更", "无Ascan": "无轨迹", "妥投未收到": "妥投未收到", "申请POD": "妥投未收到", "轨迹异常": "物流异常退回"}


def _dict(r: ParcelFollowup, names: dict) -> dict:
    return {"id": r.id, "owner_id": r.owner_id, "owner_name": r.owner_name, "occurred_on": r.occurred_on,
            "store_id": r.store_id, "store_name": names.get(r.store_id) if r.store_id else None,
            "conversation_id": r.conversation_id, "buyer_key": r.buyer_key, "order_id": r.order_id, "sku": r.sku,
            "reason": r.reason, "reason_note": r.reason_note, "carrier": r.carrier, "tracking_no": r.tracking_no,
            "action": r.action, "result": r.result, "reship_tracking_no": r.reship_tracking_no,
            "amount": r.amount, "currency": r.currency, "ticket_no": r.ticket_no, "next_follow_on": r.next_follow_on,
            "status": r.status, "note": r.note, "created_at": iso(r.created_at), "updated_at": iso(r.updated_at),
            "updated_by": r.updated_by, "closed_at": iso(r.closed_at),
            "origin": r.origin or "MANUAL", "next_step": r.next_step, "can_close": bool(r.can_close)}


def _mine(q, user: CurrentUser):
    """普通账号只看自己的；管理员全部。"""
    return q if user.is_admin else q.where(ParcelFollowup.owner_id == user.id)


def _load(db: Session, row_id: int, user: CurrentUser) -> ParcelFollowup:
    r = db.get(ParcelFollowup, row_id)
    if r is None or (not user.is_admin and r.owner_id != user.id):
        raise HTTPException(404, "记录不存在或不是你负责的")
    return r


def _names(db: Session) -> dict:
    return dict(db.execute(select(Store.id, Store.name)).all())


class FollowupBody(BaseModel):
    occurred_on: str | None = None
    store_id: str | None = Field(default=None, max_length=64)
    conversation_id: int | None = None
    buyer_key: str | None = Field(default=None, max_length=128)
    order_id: str | None = Field(default=None, max_length=64)
    sku: str | None = Field(default=None, max_length=200)
    reason: str | None = None
    reason_note: str | None = Field(default=None, max_length=300)
    carrier: str | None = Field(default=None, max_length=64)
    tracking_no: str | None = Field(default=None, max_length=64)
    action: str | None = None
    result: str | None = Field(default=None, max_length=500)
    reship_tracking_no: str | None = Field(default=None, max_length=64)
    amount: float | None = Field(default=None, ge=0, le=100000)
    currency: str | None = Field(default=None, max_length=8)
    ticket_no: str | None = Field(default=None, max_length=200)
    next_follow_on: str | None = None
    status: str | None = None
    note: str | None = Field(default=None, max_length=5000)

    @field_validator("occurred_on", "next_follow_on")
    @classmethod
    def _date(cls, v):
        if v in (None, ""):
            return None
        if not DATE_RE.match(v):
            raise ValueError("日期格式应为 YYYY-MM-DD")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        if v is not None and v not in REASONS:
            raise ValueError(f"原因类型只能是：{'、'.join(REASONS)}")
        return v

    @field_validator("action")
    @classmethod
    def _action(cls, v):
        if v in (None, ""):
            return None
        if v not in ACTIONS:
            raise ValueError(f"处理方式只能是：{'、'.join(ACTIONS)}")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError("状态只能是 OPEN（跟进中）或 DONE（已完结）")
        return v


TEXT_FIELDS = ("store_id", "buyer_key", "order_id", "sku", "reason_note", "carrier", "tracking_no", "result",
               "reship_tracking_no", "currency", "ticket_no", "note")


def _apply(r: ParcelFollowup, body: FollowupBody, fields: set[str], db: Session) -> None:
    for k in fields:
        v = getattr(body, k)
        if k in TEXT_FIELDS:
            v = (v or "").strip() or None
        if k == "currency" and v:
            v = v.upper()
        setattr(r, k, v)
    if "conversation_id" in fields and r.conversation_id:
        conv = db.get(Conversation, r.conversation_id)
        if conv is None:
            raise HTTPException(422, "关联的会话不存在")
        check_store(conv.store_id)
        r.store_id = r.store_id or conv.store_id
        r.buyer_key = r.buyer_key or conv.buyer_key
    if r.store_id:
        check_store(r.store_id)


@router.get("")
def list_followups(request: Request, status: str | None = "OPEN", reason: str | None = None, store_id: str | None = None,
                   owner_id: int | None = None, q: str | None = None, conversation_id: int | None = None,
                   origin: str | None = None, db: Session = Depends(get_db)):
    """列表（新的在前）；附各状态数量、原因与处理方式选项，管理员另附负责人列表。"""
    user = current_user(request)
    base = _mine(select(ParcelFollowup), user)
    if user.is_admin and owner_id:
        base = base.where(ParcelFollowup.owner_id == owner_id)
    if conversation_id:
        base = base.where(ParcelFollowup.conversation_id == conversation_id)
    if reason:
        base = base.where(ParcelFollowup.reason == reason)
    if origin == "AUTO":
        base = base.where(ParcelFollowup.origin == "AUTO")
    elif origin == "MANUAL":
        base = base.where(ParcelFollowup.origin.is_(None))
    if store_id:
        base = base.where(ParcelFollowup.store_id == store_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(*(c.ilike(like) for c in (
            ParcelFollowup.buyer_key, ParcelFollowup.order_id, ParcelFollowup.tracking_no, ParcelFollowup.reship_tracking_no,
            ParcelFollowup.sku, ParcelFollowup.ticket_no, ParcelFollowup.reason_note, ParcelFollowup.result,
            ParcelFollowup.next_step))))
    counts = dict(db.execute(base.with_only_columns(ParcelFollowup.status, func.count()).group_by(ParcelFollowup.status)).all())
    rows = db.scalars((base.where(ParcelFollowup.status == status) if status else base)
                      .order_by(ParcelFollowup.occurred_on.desc().nulls_last(), ParcelFollowup.id.desc()).limit(2000)).all()
    names = _names(db)
    today = datetime.now().date().isoformat()
    out = {"items": [_dict(r, names) for r in rows], "counts": {s: counts.get(s, 0) for s in STATUSES},
           "due": db.scalar(_mine(select(func.count()).select_from(ParcelFollowup), user).where(
               ParcelFollowup.status == "OPEN", ParcelFollowup.next_follow_on <= today)) or 0,
           "reasons": REASONS, "actions": ACTIONS, "is_admin": user.is_admin}
    if user.is_admin:
        out["owners"] = [{"id": i, "name": n} for i, n in db.execute(
            select(ParcelFollowup.owner_id, func.max(ParcelFollowup.owner_name)).group_by(ParcelFollowup.owner_id)).all()]
    return out


@router.post("")
def create_followup(body: FollowupBody, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if not body.reason:
        raise HTTPException(422, "请选择原因类型")
    r = ParcelFollowup(owner_id=user.id, owner_name=user.display_name[:64], status=body.status or "OPEN",
                       occurred_on=body.occurred_on or datetime.now().date().isoformat(), reason=body.reason)
    _apply(r, body, body.model_fields_set - {"status", "occurred_on", "reason"}, db)
    if r.status == "DONE":
        r.closed_at = utcnow()
    db.add(r)
    db.flush()
    db.add(AuditEvent(actor=user.display_name[:64], action="PARCEL_FOLLOWUP_CREATED", store_id=r.store_id,
                      conversation_id=r.conversation_id, detail={"id": r.id, "reason": r.reason, "order_id": r.order_id}))
    return _dict(r, _names(db))


@router.patch("/{row_id}")
def update_followup(row_id: int, body: FollowupBody, request: Request, db: Session = Depends(get_db)):
    """只改传了的字段；状态改成已完结时记完结时间，改回跟进中清掉。"""
    user = current_user(request)
    r = _load(db, row_id, user)
    fields = set(body.model_fields_set)
    if "reason" in fields and not body.reason:
        raise HTTPException(422, "原因类型不能为空")
    if "status" in fields and body.status and body.status != r.status:
        r.closed_at = utcnow() if body.status == "DONE" else None
        if body.status == "DONE":
            r.next_step = None
    _apply(r, body, fields, db)
    if "status" in fields and not body.status:
        r.status = "OPEN"
    r.updated_at, r.updated_by = utcnow(), user.display_name[:64]
    return _dict(r, _names(db))


@router.delete("/{row_id}")
def delete_followup(row_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    r = _load(db, row_id, user)
    db.add(AuditEvent(actor=user.display_name[:64], action="PARCEL_FOLLOWUP_DELETED", store_id=r.store_id,
                      conversation_id=r.conversation_id, detail={"id": r.id, "order_id": r.order_id, "reason": r.reason}))
    db.delete(r)
    return {"removed": row_id}


@router.get("/prefill")
def prefill(conversation_id: int, db: Session = Depends(get_db)):
    """从会话新建时预填：店铺、买家、订单（会话商品对应的订单，否则买家最近一单）、SKU、物流商、跟踪号、查件单号、原因。
    全部读库（订单行、发货通知、查件单），不实时查 eBay。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    buyer = (conv.buyer_key or "").lower()
    out: dict = {"conversation_id": conv.id, "store_id": conv.store_id, "buyer_key": conv.buyer_key,
                 "occurred_on": datetime.now().date().isoformat()}
    lines = db.scalars(select(EbayOrderLine).where(EbayOrderLine.store_id == conv.store_id, EbayOrderLine.buyer == buyer)
                       .order_by(EbayOrderLine.created_at.desc().nulls_last()).limit(50)).all() if buyer else []
    line = next((x for x in lines if conv.item_id and x.item_id == conv.item_id), None) or (lines[0] if lines else None)
    ticket = db.scalar(select(LogisticsTicket).where(LogisticsTicket.conversation_id == conv.id)
                       .order_by(LogisticsTicket.id.desc()).limit(1))
    order_id = line.order_id if line else (ticket.reference_no if ticket else None)
    if order_id:
        out["order_id"] = order_id
        out["sku"] = "+".join(dict.fromkeys(x.sku for x in lines if x.order_id == order_id and x.sku)) or None
        ticket = ticket or db.scalar(select(LogisticsTicket).where(LogisticsTicket.reference_no == order_id)
                                     .order_by(LogisticsTicket.id.desc()).limit(1))
        notice = db.scalar(select(ShipNotice).where(ShipNotice.store_id == conv.store_id, ShipNotice.order_id == order_id))
        pkg = next(iter(notice.packages or []), None) if notice else None
        if pkg:
            out["carrier"], out["tracking_no"] = pkg.get("carrier"), pkg.get("tracking_no")
    if ticket:
        out["tracking_no"] = out.get("tracking_no") or ticket.tracking_no
        if not (ticket.io_code or "").startswith("DRAFT-"):
            out["ticket_no"] = ticket.io_code
        out["reason"] = next((v for k, v in TICKET_REASON.items() if k in (ticket.type_name or "")), None)
    out["existing"] = db.scalar(select(func.count()).select_from(ParcelFollowup).where(
        ParcelFollowup.conversation_id == conv.id)) or 0
    return {k: v for k, v in out.items() if v not in (None, "")} | {"existing": out["existing"]}
