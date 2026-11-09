"""议价接口：查有感兴趣买家的商品、给他们发折扣报价、发送记录（按店铺范围）。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import offers
from ..auth import allowed_store_ids, check_store, current_user
from ..db import get_db, utcnow
from ..models import OfferRecord, Store

router = APIRouter(prefix="/internal/offers", tags=["offers"])


def _store(db: Session, store_id: str) -> Store:
    check_store(store_id)
    s = db.get(Store, store_id)
    if s is None:
        raise HTTPException(404, "店铺不存在")
    return s


def _market(m: str) -> str:
    m = (m or "").upper()
    if m not in offers.MARKETPLACES:
        raise HTTPException(422, f"站点要是 {', '.join(offers.MARKETPLACES)} 之一")
    return m


@router.get("/stores")
def stores(db: Session = Depends(get_db)):
    """可以议价的店铺（对接了 eBay 秘钥的），以及能不能真实发报价。"""
    from ..security.store_credentials import ebay_store_rows

    allowed = allowed_store_ids()
    ids = [r.store_id for r in ebay_store_rows(db) if allowed is None or r.store_id in allowed]
    out = []
    for s in db.scalars(select(Store).where(Store.id.in_(ids)).order_by(Store.name)):
        out.append({"id": s.id, "name": s.name, "marketplace_id": s.default_marketplace_id, "live_reason": offers.live_reason(s)})
    return {"stores": out, "marketplaces": offers.MARKETPLACES, "duration_days": offers._duration["value"],
            "min_discount": offers.MIN_DISCOUNT, "max_discount": offers.MAX_DISCOUNT, "max_message": offers.MAX_MESSAGE}


@router.get("/eligible")
def eligible(store_id: str, marketplace_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    """这个店铺在这个站点上有感兴趣买家的商品；每个附上最近 7 天发过的报价（避免重复发）。"""
    store = _store(db, store_id)
    mk = _market(marketplace_id)
    try:
        ids = offers.find_eligible(store.id, mk, refresh=refresh)
    except offers.OfferError as e:
        raise HTTPException(502, str(e))
    since = utcnow() - timedelta(days=7)
    recent: dict[str, OfferRecord] = {}
    if ids:
        for r in db.scalars(select(OfferRecord).where(OfferRecord.store_id == store.id, OfferRecord.listing_id.in_(ids),
                                                      OfferRecord.status == "SENT", OfferRecord.sent_at >= since)
                            .order_by(OfferRecord.sent_at)):
            recent[r.listing_id] = r
    return {"store_id": store.id, "marketplace_id": mk, "live_reason": offers.live_reason(store),
            "items": [{"listing_id": i, "last_offer": offers.record_dict(recent[i]) if i in recent else None} for i in ids]}


@router.get("/listing")
def listing(store_id: str, listing_id: str, db: Session = Depends(get_db)):
    """列表每行的商品信息（标题、价格、主图、SKU、可售数量），按需加载。"""
    from ..sources import get_source_for_store

    store = _store(db, store_id)
    try:
        l = get_source_for_store(store.id).get_listing(store.id, listing_id) or {}
    except Exception as e:  # noqa: BLE001
        return {"listing_id": listing_id, "error": f"{type(e).__name__}"}
    variations = l.get("variations") or []
    return {"listing_id": listing_id, "title": l.get("title"), "price": l.get("price"), "picture_url": l.get("picture_url"),
            "sku": l.get("sku") or (variations[0].get("sku") if variations else None), "url": l.get("url"),
            "quantity_available": offers.listing_quantity(l), "error": None}


class Price(BaseModel):
    value: str = Field(max_length=32)
    currency: str = Field(min_length=3, max_length=3)


class SendBody(BaseModel):
    store_id: str
    marketplace_id: str
    listing_ids: list[str] = Field(min_length=1, max_length=50)
    discount_percentage: str | None = None
    price: Price | None = None
    message: str | None = Field(default=None, max_length=offers.MAX_MESSAGE)


@router.post("/send")
def send(body: SendBody, request: Request, db: Session = Depends(get_db)):
    """给勾选商品的感兴趣买家发报价（真实发到 eBay，一个商品一次请求）。返回每个商品的结果。"""
    store = _store(db, body.store_id)
    try:
        results = offers.send(db, store, _market(body.marketplace_id), body.listing_ids,
                              discount=body.discount_percentage, price=body.price.model_dump() if body.price else None,
                              message=body.message, actor=current_user(request).display_name)
    except offers.OfferError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return {"results": results, "sent": sum(1 for r in results if r["status"] == "SENT"),
            "failed": sum(1 for r in results if r["status"] != "SENT")}


@router.get("/history")
def history(store_id: str | None = None, days: int = 30, db: Session = Depends(get_db)):
    """最近的发送记录（默认 30 天，最多 500 条）。"""
    q = select(OfferRecord).where(OfferRecord.sent_at >= utcnow() - timedelta(days=max(1, min(days, 366))))
    allowed = allowed_store_ids()
    if allowed is not None:
        q = q.where(OfferRecord.store_id.in_(allowed))
    if store_id:
        check_store(store_id)
        q = q.where(OfferRecord.store_id == store_id)
    names = dict(db.execute(select(Store.id, Store.name)).all())
    return [{**offers.record_dict(r), "store_name": names.get(r.store_id, r.store_id)}
            for r in db.scalars(q.order_by(OfferRecord.sent_at.desc()).limit(500))]

