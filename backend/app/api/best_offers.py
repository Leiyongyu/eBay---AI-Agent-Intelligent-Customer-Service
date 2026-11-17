"""Best offer 接口：买家报价列表、计数、立即刷新、回应（接受 / 还价 / 拒绝，真实写操作，只有对接 eBay 的店铺）。按店铺范围。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import allowed_store_ids, current_user
from .. import best_offers as offers_mod
from ..best_offers import OPEN, STATUS_LABELS, TYPE_LABELS, OfferError, can_respond, worker
from ..db import get_db, utcnow
from ..models import BestOffer, Conversation, Store
from .deps import iso

router = APIRouter(prefix="/internal/best-offers", tags=["best-offers"])


def _scoped(q, store_id: str | None):
    allowed = allowed_store_ids()
    if allowed is not None:
        q = q.where(BestOffer.store_id.in_(allowed))
    if store_id:
        q = q.where(BestOffer.store_id == store_id)
    return q


def _dict(o: BestOffer, store: Store | None, conv_id: int | None) -> dict:
    now = utcnow()
    pending = o.status in OPEN and not (o.expires_at and o.expires_at < now)
    return {"id": o.id, "store_id": o.store_id, "store_name": store.name if store else o.store_id,
            "marketplace_id": store.default_marketplace_id if store else None,
            "best_offer_id": o.best_offer_id, "item_id": o.item_id, "title": o.title, "picture_url": o.picture_url,
            "bin_price": o.bin_price, "currency": o.currency, "price": o.price, "quantity": o.quantity,
            "percent": round(o.price / o.bin_price * 100) if o.price and o.bin_price else None,
            "buyer": o.buyer, "buyer_feedback": o.buyer_feedback, "status": o.status,
            "status_label": STATUS_LABELS.get(o.status or "", o.status), "pending": pending,
            "offer_type": o.offer_type, "type_label": TYPE_LABELS.get(o.offer_type or "", o.offer_type),
            "buyer_message": o.buyer_message, "seller_message": o.seller_message,
            "expires_at": iso(o.expires_at), "hours_left": round((o.expires_at - now).total_seconds() / 3600, 1) if o.expires_at else None,
            "first_seen_at": iso(o.first_seen_at), "responded_action": o.responded_action, "responded_by": o.responded_by,
            "responded_at": iso(o.responded_at), "counter_price": o.counter_price, "response_message": o.response_message,
            "response_error": o.response_error, "can_respond": pending and can_respond(store) is None,
            "respond_blocked": None if not pending else can_respond(store), "conversation_id": conv_id,
            "item_url": f"https://www.ebay.com/itm/{o.item_id}" if o.item_id else None}


@router.get("")
def list_offers(tab: str = "pending", store_id: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    """tab：pending 待处理 / done 已处理（接受、还价、拒绝、过期、结束）/ all 全部。待处理的按过期时间（快过期的在前）。"""
    query = _scoped(select(BestOffer), store_id)
    now = utcnow()
    if tab == "pending":
        query = query.where(BestOffer.status.in_(OPEN), (BestOffer.expires_at.is_(None)) | (BestOffer.expires_at >= now))
        query = query.order_by(BestOffer.expires_at.is_(None), BestOffer.expires_at)
    else:
        if tab == "done":
            query = query.where((~BestOffer.status.in_(OPEN)) | (BestOffer.expires_at < now))
        query = query.order_by(BestOffer.first_seen_at.desc())
    if q and q.strip():
        like = "%" + q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(func.lower(BestOffer.buyer).like(like, escape="\\") | BestOffer.item_id.like(like, escape="\\")
                            | func.lower(BestOffer.title).like(like, escape="\\"))
    offers = db.scalars(query.limit(300)).all()
    stores = {s.id: s for s in db.scalars(select(Store))}
    convs = {}
    pairs = {(o.store_id, o.buyer) for o in offers if o.buyer}
    if pairs:
        for cid, sid, b in db.execute(select(Conversation.id, Conversation.store_id, func.lower(Conversation.buyer_key)).where(
                Conversation.store_id.in_({p[0] for p in pairs}), func.lower(Conversation.buyer_key).in_({p[1] for p in pairs}))):
            convs.setdefault((sid, b), cid)
    return [_dict(o, stores.get(o.store_id), convs.get((o.store_id, o.buyer))) for o in offers]


@router.get("/counts")
def counts(store_id: str | None = None, db: Session = Depends(get_db)):
    now = utcnow()
    base = _scoped(select(func.count(BestOffer.id)), store_id)
    pending = db.scalar(base.where(BestOffer.status.in_(OPEN), (BestOffer.expires_at.is_(None)) | (BestOffer.expires_at >= now))) or 0
    total = db.scalar(_scoped(select(func.count(BestOffer.id)), store_id)) or 0
    return {"pending": pending, "done": total - pending, "all": total,
            "sync": {"last_at": iso(worker.last_at), "running": worker.running_now, "last": worker.last}}


@router.post("/refresh")
def refresh():
    """马上从 eBay 拉一次全部店铺（后台进行，约 1 分钟）。"""
    return {"started": worker.run_in_background()}


class RespondBody(BaseModel):
    action: str = Field(pattern="^(Accept|Counter|Decline)$")
    price: float | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, ge=1)
    message: str | None = Field(default=None, max_length=250)
    confirmed: bool = False  # 页面上二次确认过


@router.post("/{offer_id}/respond")
def respond_offer(offer_id: int, body: RespondBody, request: Request, db: Session = Depends(get_db)):
    """真实在 eBay 上接受 / 还价 / 拒绝这条报价（接受后买家即成交）。"""
    o = db.get(BestOffer, offer_id)
    allowed = allowed_store_ids()
    if o is None or (allowed is not None and o.store_id not in allowed):
        raise HTTPException(404, "报价不存在")
    if not body.confirmed:
        raise HTTPException(422, "请先在页面上确认")
    try:
        offers_mod.respond(db, o, body.action, current_user(request).display_name, price=body.price, quantity=body.quantity,
                message=body.message)
    except OfferError as e:
        db.commit()  # 失败原因也记下来
        raise HTTPException(e.status_code, e.message) from None
    db.commit()
    return _dict(o, db.get(Store, o.store_id), None)
