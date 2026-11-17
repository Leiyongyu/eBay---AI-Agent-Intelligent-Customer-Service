"""客户管理：按买家汇总 eBay 订单（只读拉取），以及系统内的黑名单。

- 订单：随售后管理一起定时拉（ebay_cases.sync_all），第一次拉近 ORDERS_SYNC_DAYS 天，之后按最后修改时间增量；
  只存汇总要用的字段（买家账号、国家、金额、状态、商品），不存买家姓名、地址、电话；
- 订单里的每件商品（SKU、数量、金额、交易号）另存 ebay_order_lines，售后管理列表直接读；
  店铺第一次还没有商品明细时，把整个 ORDERS_SYNC_DAYS 窗口重拉一遍补上；
- 黑名单：本系统内的名单（eBay 没有开放“屏蔽买家”接口，要真正屏蔽需在各店铺 eBay 后台手动加）。
  黑名单买家来信 AI 不起草回复、不发“已收到”回执，直接交人工；会话、售后记录、客户列表上标红。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import BuyerBlacklist, EbayOrder, EbayOrderLine, EbaySyncState

log = logging.getLogger(__name__)
ORDER_STATE = "ORDERS"  # ebay_sync_state.conversation_type 借用一行记订单同步游标
OVERLAP = timedelta(hours=2)
PAGE, MAX_PAGES = 200, 50


def _f(x) -> float | None:
    try:
        return float((x or {}).get("value"))
    except (TypeError, ValueError):
        return None


def parse_order(o: dict) -> dict:
    from .ebay_cases import _dt

    total = (o.get("pricingSummary") or {}).get("total") or {}
    ship = ((o.get("fulfillmentStartInstructions") or [{}])[0].get("shippingStep") or {}).get("shipTo") or {}
    country = (ship.get("contactAddress") or {}).get("countryCode") or \
        (((o.get("buyer") or {}).get("taxAddress") or {}).get("countryCode"))
    line = (o.get("lineItems") or [{}])[0]
    return {"order_id": o.get("orderId"), "buyer": ((o.get("buyer") or {}).get("username") or "").lower() or None,
            "country": country, "created_at": _dt(o.get("creationDate")), "ebay_updated_at": _dt(o.get("lastModifiedDate")),
            "total": _f(total), "currency": total.get("currency"), "status": o.get("orderFulfillmentStatus"),
            "payment_status": o.get("orderPaymentStatus"),
            "refunded": bool((o.get("paymentSummary") or {}).get("refunds")),
            "cancelled": ((o.get("cancelStatus") or {}).get("cancelState") or "") == "CANCELED",
            "item_id": line.get("legacyItemId"), "item_title": (line.get("title") or "")[:300] or None,
            "items": sum(int(li.get("quantity") or 0) for li in o.get("lineItems") or []),
            "skus": ",".join(dict.fromkeys(li["sku"] for li in o.get("lineItems") or [] if li.get("sku")))[:500] or None,
            "item_ids": ",".join(dict.fromkeys(str(li["legacyItemId"]) for li in o.get("lineItems") or []
                                               if li.get("legacyItemId")))[:300] or None,
            "lines": [parse_line(li) for li in o.get("lineItems") or [] if li.get("lineItemId")],
            "legacy_order_id": o.get("legacyOrderId")}


def parse_line(li: dict) -> dict:
    cost = li.get("lineItemCost") or {}
    item_id = str(li["legacyItemId"]) if li.get("legacyItemId") else None
    line_id = str(li.get("lineItemId"))
    try:
        qty = int(li.get("quantity")) if li.get("quantity") is not None else None
    except (TypeError, ValueError):
        qty = None
    return {"line_item_id": line_id, "line_key": f"{item_id}-{line_id}" if item_id else None, "item_id": item_id,
            "sku": (li.get("sku") or "")[:100] or None, "title": (li.get("title") or "")[:300] or None, "quantity": qty,
            "price_value": str(cost["value"]) if cost.get("value") not in (None, "") else None,
            "price_currency": cost.get("currency")}


def save_orders(db: Session, store_id: str, rows: list[dict]) -> int:
    ids = [r["order_id"] for r in rows if r.get("order_id")]
    existing = {o.order_id: o for o in db.scalars(select(EbayOrder).where(EbayOrder.store_id == store_id,
                                                                          EbayOrder.order_id.in_(ids)))} if ids else {}
    old_lines: dict[tuple[str, str], EbayOrderLine] = {}
    for i in range(0, len(ids), 500):
        for ln in db.scalars(select(EbayOrderLine).where(EbayOrderLine.store_id == store_id,
                                                         EbayOrderLine.order_id.in_(ids[i:i + 500]))):
            old_lines[(ln.order_id, ln.line_item_id)] = ln
    new = 0
    for r in rows:
        if not r.get("order_id"):
            continue
        r = dict(r)
        lines, legacy = r.pop("lines", None), r.pop("legacy_order_id", None)
        o = existing.get(r["order_id"])
        if o is None:
            o = EbayOrder(store_id=store_id, order_id=r["order_id"])
            db.add(o)
            existing[r["order_id"]] = o
            new += 1
        for k, v in r.items():
            if k != "order_id":
                setattr(o, k, v)
        o.synced_at = utcnow()
        for li in lines or []:  # 每件商品：有就更新，没有就新增
            ln = old_lines.get((r["order_id"], li["line_item_id"]))
            if ln is None:
                ln = EbayOrderLine(store_id=store_id, order_id=r["order_id"], line_item_id=li["line_item_id"])
                db.add(ln)
                old_lines[(r["order_id"], li["line_item_id"])] = ln
            for k, v in li.items():
                setattr(ln, k, v)
            ln.legacy_order_id, ln.buyer, ln.created_at = legacy, r.get("buyer"), r.get("created_at")
    db.flush()
    return new


def needs_lines_backfill(db: Session, store_id: str) -> bool:
    """店铺已经有订单、但还没有任何商品明细（加明细表之前同步的）：要把整个窗口重拉一遍。"""
    has_orders = db.scalar(select(EbayOrder.id).where(EbayOrder.store_id == store_id).limit(1)) is not None
    has_lines = db.scalar(select(EbayOrderLine.id).where(EbayOrderLine.store_id == store_id).limit(1)) is not None
    return has_orders and not has_lines


def sync_orders(store_id: str, marketplace: str | None) -> dict:
    """拉一个店铺的订单（只读 getOrders）：第一次近 ORDERS_SYNC_DAYS 天，之后按最后修改时间增量。"""
    from .sources.ebay import EbayReadOnlyApi

    started = utcnow()
    with session_scope() as db:
        st = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store_id,
                                                   EbaySyncState.conversation_type == ORDER_STATE))
        since = (st.last_success_at - OVERLAP) if st and st.last_success_at and not needs_lines_backfill(db, store_id) else \
            started - timedelta(days=get_settings().orders_sync_days)
    api = EbayReadOnlyApi(store_id)
    flt = f"lastmodifieddate:[{since.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..]"
    rows: list[dict] = []
    for page in range(MAX_PAGES):
        d = api.get("/sell/fulfillment/v1/order", {"limit": PAGE, "offset": page * PAGE, "filter": flt},
                    marketplace=marketplace) or {}
        items = d.get("orders") or []
        rows += [parse_order(o) for o in items]
        if len(items) < PAGE or not d.get("next"):
            break
    with session_scope() as db:
        new = save_orders(db, store_id, rows)
        st = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store_id,
                                                   EbaySyncState.conversation_type == ORDER_STATE))
        if st is None:
            st = EbaySyncState(store_id=store_id, conversation_type=ORDER_STATE)
            db.add(st)
        st.last_started_at, st.last_success_at, st.last_error = started, started, None
        st.last_stats = {"orders": len(rows), "new": new}
    return {"orders": len(rows), "new": new}


# ------------------------------------------------------------------ 黑名单
def blacklist_entry(db: Session, store_id: str | None, buyer: str | None) -> BuyerBlacklist | None:
    """这个买家在这个店铺（或全部店铺）是否在黑名单里。"""
    if not buyer:
        return None
    q = select(BuyerBlacklist).where(BuyerBlacklist.buyer == buyer.strip().lower())
    q = q.where(or_(BuyerBlacklist.store_id.is_(None), BuyerBlacklist.store_id == store_id)) if store_id else q
    return db.scalar(q.order_by(BuyerBlacklist.store_id.is_(None).desc()).limit(1))


def blacklist_map(db: Session, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], BuyerBlacklist]:
    """批量查（店铺, 买家小写）→ 黑名单记录：“全部店铺”的优先，其次这个店铺的（与工作台、详情页、blacklist_entry 一致）。"""
    buyers = {b for _, b in pairs if b}
    if not buyers:
        return {}
    rows = db.scalars(select(BuyerBlacklist).where(BuyerBlacklist.buyer.in_(buyers))).all()
    out = {}
    for sid, b in pairs:
        hit = next((r for r in rows if r.buyer == b and r.store_id is None), None) or \
            next((r for r in rows if r.buyer == b and r.store_id == sid), None)
        if hit:
            out[(sid, b)] = hit
    return out


def blacklist_dict(e: BuyerBlacklist | None) -> dict | None:
    from .api.deps import iso

    if e is None:
        return None
    return {"id": e.id, "buyer": e.buyer, "store_id": e.store_id, "reason": e.reason, "created_by": e.created_by,
            "created_at": iso(e.created_at)}


def buyer_summary(db: Session, buyer: str, stores: set[str] | None) -> dict:
    """这个买家在（当前账号能看的）全部店铺的订单汇总：店铺分布、金额、首末单、退款取消、售后次数、最近订单。
    来自本地的 ebay_orders / ebay_cases（随售后管理定时拉，全部店铺），不用逐店实时查。"""
    from .api.deps import iso
    from .models import EbayCase, Store

    b = (buyer or "").strip().lower()
    scope = (EbayOrder.store_id.in_(stores),) if stores is not None else ()
    orders = db.scalars(select(EbayOrder).where(EbayOrder.buyer == b, *scope).order_by(EbayOrder.created_at.desc())).all()
    names = dict(db.execute(select(Store.id, Store.name)).all())
    per_store: dict[str, dict] = {}
    amounts: dict[str, float] = {}
    for o in orders:
        st = per_store.setdefault(o.store_id, {"store_id": o.store_id, "store_name": names.get(o.store_id) or o.store_id,
                                               "orders": 0, "amounts": {}})
        st["orders"] += 1
        if o.total is not None:
            st["amounts"][o.currency or "?"] = round(st["amounts"].get(o.currency or "?", 0) + o.total, 2)
            amounts[o.currency or "?"] = round(amounts.get(o.currency or "?", 0) + o.total, 2)
    cscope = (EbayCase.store_id.in_(stores),) if stores is not None else ()
    kinds = dict(db.execute(select(EbayCase.kind, func.count()).where(func.lower(EbayCase.buyer) == b, *cscope)
                            .group_by(EbayCase.kind)).all())
    negative = db.scalar(select(func.count()).select_from(EbayCase).where(
        func.lower(EbayCase.buyer) == b, EbayCase.kind == "FEEDBACK", EbayCase.rating == "Negative", *cscope)) or 0
    times = [o.created_at for o in orders if o.created_at]
    return {
        "buyer": b, "order_count": len(orders), "amounts": amounts, "stores": sorted(per_store.values(), key=lambda x: -x["orders"]),
        "first_order_at": iso(min(times)) if times else None, "last_order_at": iso(max(times)) if times else None,
        "refunded_orders": sum(1 for o in orders if o.refunded), "cancelled_orders": sum(1 for o in orders if o.cancelled),
        "returns": kinds.get("RETURN", 0), "disputes": kinds.get("CASE", 0) + kinds.get("INQUIRY", 0) + kinds.get("PAYMENT_DISPUTE", 0),
        "negative_feedback": negative,
        "orders": [{"store_id": o.store_id, "store_name": names.get(o.store_id) or o.store_id, "order_id": o.order_id,
                    "created_at": iso(o.created_at), "total": o.total, "currency": o.currency, "status": o.status,
                    "item_id": o.item_id, "item_title": o.item_title, "skus": o.skus, "refunded": o.refunded,
                    "cancelled": o.cancelled} for o in orders[:100]],
        "orders_synced_days": get_settings().orders_sync_days,
    }


def order_count(db: Session) -> int:
    return db.scalar(select(func.count(EbayOrder.id))) or 0
