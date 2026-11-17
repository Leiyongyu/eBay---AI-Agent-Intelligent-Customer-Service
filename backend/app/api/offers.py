"""议价接口：查有感兴趣买家的商品、给他们发折扣报价、发送记录（按店铺范围）。

议价店铺的商品由 offer_sync 定时同步进 offer_items：列表、商品信息直接读库（同步过的店铺）；
页面点“刷新”（refresh=true）或店铺还没同步过时实时查 eBay，并顺手写回库里。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import offers
from ..auth import allowed_store_ids, check_store, current_user
from ..db import get_db, utcnow
from .deps import iso
from ..models import EbaySyncState, OfferItem, OfferRecord, Store

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


def _synced_at(db: Session, store_id: str):
    from ..offer_sync import SYNC_TYPE

    return db.scalar(select(EbaySyncState.last_success_at).where(EbaySyncState.store_id == store_id,
                                                                 EbaySyncState.conversation_type == SYNC_TYPE))


def _listing_dict(r: OfferItem) -> dict:
    return {"listing_id": r.listing_id, "title": r.title,
            "price": {"value": r.price_value, "currency": r.price_currency} if r.price_value else None,
            "picture_url": r.picture_url, "sku": r.sku, "url": r.url, "quantity_available": r.quantity_available,
            "listing_status": r.listing_status, "synced_at": iso(r.detail_synced_at), "error": r.detail_error}


@router.get("/eligible")
def eligible(store_id: str, marketplace_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    """这个店铺在这个站点上有感兴趣买家的商品（同步过的店铺读库，带商品信息；refresh 或没同步过时实时查 eBay 并写回库）；
    每个附上最近 7 天发过的报价（避免重复发）。"""
    from .. import offer_sync

    store = _store(db, store_id)
    mk = _market(marketplace_id)
    synced = _synced_at(db, store.id)
    source = "db"
    if refresh or synced is None:
        try:
            ids = offers.find_eligible(store.id, mk, refresh=refresh)
        except offers.OfferError as e:
            raise HTTPException(502, str(e))
        offer_sync.save_eligible(db, store.id, mk, ids)
        db.commit()
        source = "live"
    rows = {r.listing_id: r for r in db.scalars(select(OfferItem).where(
        OfferItem.store_id == store.id, OfferItem.marketplace_id == mk, OfferItem.eligible.is_(True))
        .order_by(OfferItem.id))}  # 第一次出现的顺序（eBay 返回的顺序），新出现的排在后面
    ids = list(rows)
    since = utcnow() - timedelta(days=7)
    recent: dict[str, OfferRecord] = {}
    if ids:
        for r in db.scalars(select(OfferRecord).where(OfferRecord.store_id == store.id, OfferRecord.listing_id.in_(ids),
                                                      OfferRecord.status == "SENT", OfferRecord.sent_at >= since)
                            .order_by(OfferRecord.sent_at)):
            recent[r.listing_id] = r
    return {"store_id": store.id, "marketplace_id": mk, "live_reason": offers.live_reason(store), "source": source,
            "synced_at": iso(_synced_at(db, store.id)),
            "items": [{"listing_id": i, "last_offer": offers.record_dict(recent[i]) if i in recent else None,
                       "listing": _listing_dict(rows[i]) if rows[i].detail_synced_at else None} for i in ids]}


@router.get("/listing")
def listing(store_id: str, listing_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    """列表每行的商品信息（标题、价格、主图、SKU、可售数量）：库里同步过的直接返回，没有（或 refresh）时实时查 eBay 并写回库。"""
    from ..offer_sync import apply_detail
    from ..sources import get_source_for_store

    store = _store(db, store_id)
    rows = list(db.scalars(select(OfferItem).where(OfferItem.store_id == store.id, OfferItem.listing_id == listing_id)))
    cached = next((r for r in rows if r.detail_synced_at and not r.detail_error), None)
    if cached is not None and not refresh:
        return _listing_dict(cached)
    try:
        src = get_source_for_store(store.id)
        l = (getattr(src, "offer_brief", None) or src.get_listing)(store.id, listing_id)
    except Exception as e:  # noqa: BLE001
        return {"listing_id": listing_id, "error": f"{type(e).__name__}"}
    if rows:
        apply_detail(rows, l, utcnow())
        db.commit()
    l = l or {}
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

