"""客户管理接口：客户列表（按买家汇总订单、售后、会话）与黑名单。按店铺范围。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..auth import allowed_store_ids, current_user
from ..customers import blacklist_dict, blacklist_map
from ..db import get_db, utcnow
from ..models import AuditEvent, BuyerBlacklist, Conversation, EbayCase, EbayOrder, Store
from .deps import iso

router = APIRouter(prefix="/internal", tags=["customers"])

def _like(q: str) -> str:
    """模糊搜索：转义 % 和 _ 和反斜杠，买家名里常见的下划线按字面匹配。"""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"



def _stores(store_id: str | None) -> set[str] | None:
    allowed = allowed_store_ids()
    if store_id:
        if allowed is not None and store_id not in allowed:
            raise HTTPException(404, "店铺不存在")
        return {store_id}
    return allowed


@router.get("/customers")
def list_customers(q: str | None = None, store_id: str | None = None, blacklisted: bool = False, sort: str = "recent",
                   limit: int = 200, db: Session = Depends(get_db)):
    """按买家汇总订单（买过几次、金额、最近下单），再带上退货 / 纠纷 / 差评次数、会话、黑名单。"""
    stores = _stores(store_id)
    oq = select(EbayOrder.buyer, func.count(EbayOrder.id), func.max(EbayOrder.created_at), func.min(EbayOrder.created_at),
                func.sum(case((EbayOrder.refunded.is_(True), 1), else_=0)),
                func.sum(case((EbayOrder.cancelled.is_(True), 1), else_=0))).where(EbayOrder.buyer.is_not(None))
    if stores is not None:
        oq = oq.where(EbayOrder.store_id.in_(stores))
    if q and q.strip():
        oq = oq.where(EbayOrder.buyer.ilike(_like(q.strip().lower()), escape="\\"))
    if blacklisted:
        bl = select(BuyerBlacklist.buyer)
        oq = oq.where(EbayOrder.buyer.in_(bl))
    order_by = {"orders": func.count(EbayOrder.id).desc(), "recent": func.max(EbayOrder.created_at).desc()}.get(sort)
    rows = db.execute(oq.group_by(EbayOrder.buyer).order_by(order_by if order_by is not None else func.max(EbayOrder.created_at).desc())
                      .limit(min(limit, 500))).all()
    buyers = [r[0] for r in rows]
    if not buyers:
        return {"items": [], "total": 0}
    scope = (EbayOrder.store_id.in_(stores),) if stores is not None else ()
    # 金额按币种、店铺、国家
    money, store_of, country = {}, {}, {}
    for b, cur, tot in db.execute(select(EbayOrder.buyer, EbayOrder.currency, func.sum(EbayOrder.total)).where(
            EbayOrder.buyer.in_(buyers), *scope).group_by(EbayOrder.buyer, EbayOrder.currency)):
        money.setdefault(b, {})[cur or "?"] = round(tot or 0, 2)
    names = dict(db.execute(select(Store.id, Store.name)).all())
    for b, sid, c in db.execute(select(EbayOrder.buyer, EbayOrder.store_id, EbayOrder.country).where(
            EbayOrder.buyer.in_(buyers), *scope).order_by(EbayOrder.created_at)):
        store_of.setdefault(b, {})[sid] = names.get(sid) or sid
        if c:
            country[b] = c
    cscope = (EbayCase.store_id.in_(stores),) if stores is not None else ()
    cases = {}
    for b, kind, n, bad in db.execute(select(func.lower(EbayCase.buyer), EbayCase.kind, func.count(),
                                             func.sum(case((EbayCase.rating == "Negative", 1), else_=0))).where(
            func.lower(EbayCase.buyer).in_(buyers), *cscope).group_by(func.lower(EbayCase.buyer), EbayCase.kind)):
        cases.setdefault(b, {})[kind] = (n, bad or 0)
    conv = {}
    vscope = (Conversation.store_id.in_(stores),) if stores is not None else ()
    for cid, b in db.execute(select(Conversation.id, func.lower(Conversation.buyer_key)).where(
            func.lower(Conversation.buyer_key).in_(buyers), *vscope).order_by(Conversation.last_message_at.desc())):
        conv.setdefault(b, cid)
    bl = blacklist_map(db, {(sid, b) for b in buyers for sid in [*store_of.get(b, {}), None]})
    items = []
    for b, n, last, first, refunds, cancels in rows:
        c = cases.get(b, {})
        entries = [blacklist_dict(e) for (sid, bb), e in bl.items() if bb == b]
        items.append({"buyer": b, "country": country.get(b), "orders": n, "first_order_at": iso(first), "last_order_at": iso(last),
                      "amounts": money.get(b, {}), "stores": [{"id": k, "name": v} for k, v in store_of.get(b, {}).items()],
                      "refunded_orders": int(refunds or 0), "cancelled_orders": int(cancels or 0),
                      "returns": c.get("RETURN", (0, 0))[0], "disputes": c.get("CASE", (0, 0))[0] + c.get("INQUIRY", (0, 0))[0]
                      + c.get("PAYMENT_DISPUTE", (0, 0))[0], "negative_feedback": c.get("FEEDBACK", (0, 0))[1],
                      "conversation_id": conv.get(b),
                      "blacklist": list({e["id"]: e for e in entries}.values())})
    return {"items": items, "total": len(items)}


# ------------------------------------------------------------------ 黑名单
@router.get("/blacklist")
def list_blacklist(q: str | None = None, db: Session = Depends(get_db)):
    allowed = allowed_store_ids()
    query = select(BuyerBlacklist).order_by(BuyerBlacklist.created_at.desc())
    if q and q.strip():
        query = query.where(BuyerBlacklist.buyer.ilike(_like(q.strip().lower()), escape="\\"))
    names = dict(db.execute(select(Store.id, Store.name)).all())
    out = []
    for e in db.scalars(query):
        if allowed is not None and e.store_id is not None and e.store_id not in allowed:
            continue
        out.append({**blacklist_dict(e), "store_name": names.get(e.store_id) if e.store_id else "全部店铺"})
    return out


class BlacklistBody(BaseModel):
    buyer: str = Field(min_length=1, max_length=128)
    store_id: str | None = None  # 空 = 全部店铺
    reason: str | None = Field(default=None, max_length=500)


@router.post("/blacklist")
def add_blacklist(body: BlacklistBody, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    buyer = body.buyer.strip().lower()
    allowed = allowed_store_ids()
    if body.store_id is None and allowed is not None:
        raise HTTPException(403, "只能把买家加进自己负责店铺的黑名单（全部店铺需要管理员或负责全部店铺的账号）")
    if body.store_id is not None and allowed is not None and body.store_id not in allowed:
        raise HTTPException(404, "店铺不存在")
    exists = db.scalar(select(BuyerBlacklist).where(BuyerBlacklist.buyer == buyer, BuyerBlacklist.store_id.is_(None)
                                                    if body.store_id is None else BuyerBlacklist.store_id == body.store_id))
    if exists:
        raise HTTPException(409, "这个买家已经在黑名单里")
    e = BuyerBlacklist(buyer=buyer, store_id=body.store_id, reason=(body.reason or "").strip() or None,
                       created_by=user.display_name[:64], created_at=utcnow())
    db.add(e)
    db.add(AuditEvent(actor=user.display_name[:64], action="BUYER_BLACKLISTED", store_id=body.store_id,
                      detail={"buyer": buyer, "reason": e.reason}))
    db.commit()
    return blacklist_dict(e)


@router.delete("/blacklist/{entry_id}")
def remove_blacklist(entry_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    e = db.get(BuyerBlacklist, entry_id)
    allowed = allowed_store_ids()
    if e is None or (allowed is not None and (e.store_id is None or e.store_id not in allowed)):
        raise HTTPException(404, "记录不存在")
    db.add(AuditEvent(actor=user.display_name[:64], action="BUYER_UNBLACKLISTED", store_id=e.store_id,
                      detail={"buyer": e.buyer, "reason": e.reason}))
    db.delete(e)
    db.commit()
    return {"removed": entry_id}
