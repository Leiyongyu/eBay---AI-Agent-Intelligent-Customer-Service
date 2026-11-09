"""会话页右侧三栏：相关信息（订单、包裹）/ 客户资料（累计订单与退款）/ 历史记录（同一买家在其他店铺、其他商品的会话）。

收件人姓名、地址、电话、邮箱只在这里显示给已登录、负责该店铺的客服：不写库、不进工具结果、不发给模型。
订单来自各店铺最近 90 天的订单（eBay 店铺实时查、10 分钟缓存；演示店铺用演示数据源）。
"""
from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation

from .ebay.stores import is_ebay_store
from .sources import get_business_source

_lock = threading.Lock()
_ebay_source = None


def _ebay():
    """和其他地方共用同一个 eBay 数据源实例：订单列表缓存只有一份，不会同一店铺拉两遍。"""
    global _ebay_source
    with _lock:
        if _ebay_source is None:
            from .sources import get_ebay_source

            _ebay_source = get_ebay_source()
        return _ebay_source


def _money(m: dict | None) -> dict | None:
    if not m or m.get("value") in (None, ""):
        return None
    return {"value": str(m.get("value")), "currency": m.get("currency")}


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return Decimal(0)


def order_view(o: dict) -> dict:
    """eBay 原始订单 → 客服看的订单卡片。"""
    items = o.get("lineItems") or []
    start = (o.get("fulfillmentStartInstructions") or [{}])[0]
    step = start.get("shippingStep") or {}
    ship = step.get("shipTo") or {}
    addr = ship.get("contactAddress") or {}
    pay = o.get("paymentSummary") or {}
    payments = pay.get("payments") or []
    city = ", ".join(x for x in (addr.get("city"), addr.get("stateOrProvince")) if x)
    address = ", ".join(x for x in (addr.get("addressLine1"), addr.get("addressLine2"), city, addr.get("postalCode")) if x)
    rows = [{"item_id": li.get("legacyItemId"), "sku": li.get("sku"), "title": li.get("title"),
             "quantity": li.get("quantity"), "status": li.get("lineItemFulfillmentStatus"),
             "price": _money(li.get("lineItemCost"))}
            for li in items]
    return {
        "order_id": o.get("orderId"), "legacy_order_id": o.get("legacyOrderId"),
        "record_no": o.get("salesRecordReference"),
        "marketplace_id": (items[0] if items else {}).get("listingMarketplaceId"),
        "created_at": o.get("creationDate"),
        "paid_at": payments[0].get("paymentDate") if payments else None,
        "payment_status": o.get("orderPaymentStatus"),  # PAID / PENDING / FULLY_REFUNDED …
        "order_status": o.get("orderFulfillmentStatus"),  # NOT_STARTED / IN_PROGRESS / FULFILLED
        "cancel_state": (o.get("cancelStatus") or {}).get("cancelState"),
        "total": _money((o.get("pricingSummary") or {}).get("total")),
        "refunds": [{"amount": _money(r.get("amount")), "date": r.get("refundDate"), "status": r.get("refundStatus")}
                    for r in pay.get("refunds") or []],
        "items": rows,
        "quantity": sum(int(r["quantity"] or 0) for r in rows),
        "shipping_service": step.get("shippingServiceCode"),
        "buyer_note": o.get("buyerCheckoutNotes"),
        "ship_to": {"name": ship.get("fullName"), "company": ship.get("companyName"), "address": address or None,
                    "country": addr.get("countryCode"), "phone": (ship.get("primaryPhone") or {}).get("phoneNumber"),
                    "email": ship.get("email")},
    }


def row_view(r: dict) -> dict:
    """演示数据源的订单行（字段较少）→ 同样的卡片结构。"""
    total = r.get("total")
    return {
        "order_id": r.get("order_id"), "legacy_order_id": r.get("legacy_order_id"), "record_no": None,
        "marketplace_id": r.get("marketplace_id"), "created_at": r.get("created_at") or r.get("paid_at"),
        "paid_at": r.get("paid_at"), "payment_status": r.get("payment_status") or ("PAID" if r.get("paid_at") else None),
        "order_status": r.get("order_status"), "cancel_state": r.get("cancel_state"),
        "total": total if isinstance(total, dict) else None, "refunds": [],
        "items": [{"item_id": r.get("item_id"), "sku": r.get("sku"), "title": r.get("title"),
                   "quantity": r.get("quantity"), "status": None}],
        "quantity": int(r.get("quantity") or 0), "shipping_service": r.get("shipping_service"), "buyer_note": None,
        "ship_to": {"name": None, "company": None, "address": None, "country": r.get("ship_to_country"),
                    "phone": None, "email": None},
    }


def store_orders(store_id: str, buyer_key: str) -> list[dict]:
    """买家在一个店铺的订单，新的在前。"""
    if is_ebay_store(store_id):
        orders = [order_view(o) for o in _ebay().buyer_orders_raw(store_id, buyer_key)]
    else:
        orders = [row_view(r) for r in get_business_source().find_orders(store_id, buyer_key)]
    return sorted(orders, key=lambda o: o.get("created_at") or "", reverse=True)


def packages(store_id: str, order_id: str) -> list[dict]:
    src = _ebay() if is_ebay_store(store_id) else get_business_source()
    return src.find_packages(store_id, order_id)


def _match(orders: list[dict], item_id: str | None) -> dict | None:
    if item_id:
        for o in orders:
            if any(i.get("item_id") == item_id for i in o["items"]):
                return o
    return None


def _sum_by_currency(amounts) -> list[dict]:
    acc: dict[str, list] = {}
    for m in amounts:
        if not m:
            continue
        a = acc.setdefault(m.get("currency") or "", [Decimal(0), 0])
        a[0] += _dec(m.get("value"))
        a[1] += 1
    cent = Decimal("0.01")
    return [{"currency": c, "total": str(v[0].quantize(cent)), "count": v[1], "avg": str((v[0] / v[1]).quantize(cent))}
            for c, v in acc.items()]


def build(conv, others: list, store_names: dict[str, str], previews: dict[int, str]) -> dict:
    """conv：当前会话；others：同一买家的其他会话（已按账号的店铺范围过滤）。"""
    stores = list(dict.fromkeys([conv.store_id, *[c.store_id for c in others]]))
    by_store: dict[str, list[dict]] = {}
    errors = []
    for sid in stores:
        try:
            by_store[sid] = store_orders(sid, conv.buyer_key)
        except Exception as e:  # 某个店铺查不了订单只影响这一家
            by_store[sid] = []
            errors.append({"store_id": sid, "store_name": store_names.get(sid) or sid,
                           "error": f"查询订单失败：{type(e).__name__}: {str(e)[:160]}"})

    orders = by_store.get(conv.store_id, [])
    related = _match(orders, conv.item_id) or (orders[0] if orders else None)
    pkgs, pkg_error = [], None
    if related and related.get("order_id"):
        try:
            pkgs = packages(conv.store_id, related["order_id"])
        except Exception as e:
            pkg_error = f"查询包裹失败：{type(e).__name__}"

    every = [o for lst in by_store.values() for o in lst]
    contact = next((o["ship_to"] for o in every if o["ship_to"].get("phone") or o["ship_to"].get("email")), None) \
        or (related or {}).get("ship_to") or {}
    times = sorted(t for t in (o.get("created_at") for o in every) if t)
    refunds = [r["amount"] for o in every for r in o["refunds"]]
    history = []
    for c in others:
        m = _match(by_store.get(c.store_id, []), c.item_id)
        history.append({"id": c.id, "store_id": c.store_id, "store_name": store_names.get(c.store_id) or c.store_id,
                        "marketplace_id": c.marketplace_id, "item_id": c.item_id, "title": c.conversation_title,
                        "last_message_at": c.last_message_at, "preview": previews.get(c.id, ""),
                        "order_id": m["order_id"] if m else None})
    return {
        "buyer_key": conv.buyer_key,
        "related_order": related,
        "packages": pkgs, "packages_error": pkg_error,
        "orders": orders,  # 本店全部订单（新的在前）
        "profile": {
            "buyer_key": conv.buyer_key, "name": contact.get("name"),
            "email": contact.get("email"), "phone": contact.get("phone"),
            "stores": [{"store_id": sid, "store_name": store_names.get(sid) or sid, "orders": len(by_store[sid])}
                       for sid in stores],
            "total_quantity": sum(o["quantity"] for o in every),
            "order_count": len(every),
            "amounts": _sum_by_currency(o["total"] for o in every),
            "first_order_at": times[0] if times else None, "last_order_at": times[-1] if times else None,
            "refund_count": len(refunds), "refund_amounts": _sum_by_currency(refunds),
        },
        "history": history,
        "errors": errors,
    }
