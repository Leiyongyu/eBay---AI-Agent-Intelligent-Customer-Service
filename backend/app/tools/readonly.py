"""首批只读业务工具（第 12.2 节）。

每个工具只接收经 Schema 校验的参数；store_id、buyer_key、item_id 一律来自 RuntimeContext。
买家文字里的订单号、SKU 只作定位线索，不能跳过店铺与买家归属校验。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from ..agent.schemas import RuntimeContext
from ..config import get_settings
from ..logistics.assess import assess
from ..logistics.service import lookup as logistics_lookup
from ..sources import BusinessSource

SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderRefArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_ref: str | None = Field(default=None, max_length=40)


class SkuArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(max_length=40)


class OrderIdArgs(BaseModel):
    """order_id 必须属于当前买家在本店的订单（工具里再核对一次，模型填别人的订单号查不到）。"""

    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(max_length=40)


# 返回值约定：(status, data, evidence_ref, message)
Outcome = tuple[str, object, str | None, str | None]


def get_listing_context(ctx: RuntimeContext, src: BusinessSource, args: NoArgs) -> Outcome:
    if not ctx.item_id:
        return "NOT_FOUND", None, None, "会话没有关联刊登"
    listing = src.get_listing(ctx.store_id, ctx.item_id)
    if listing is None:
        return "NOT_FOUND", None, None, "刊登不属于当前店铺或不存在"
    data = {k: listing[k] for k in (
        "item_id", "marketplace_id", "title", "variations", "item_specifics",
        "compatibility", "handling_time", "shipping_options", "listing_status",
    )}
    ref = f"listing:{listing['item_id']}"
    if len(listing["variations"]) > 1:
        return "AMBIGUOUS", data, ref, "多变体刊登，无法确定买家指的是哪个 SKU"
    return "FOUND", data, ref, None


class ItemDetailsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle: str | None = Field(default=None, max_length=80, description="买家的车型，如 2010 Chevrolet Tahoe；用于筛选兼容车型")


COMPAT_MATCH_LIMIT = 30  # 按车型筛出的兼容记录最多给模型多少条
COMPAT_SUMMARY_LIMIT = 40  # 不带车型时，兼容车型按“品牌 车型”汇总后最多多少行


def _compat_text(row) -> str:
    return " ".join(str(v) for v in row.values()) if isinstance(row, dict) else str(row)


def _compat_summary(rows: list) -> list[str]:
    """不带车型时不把上千条兼容记录全给模型：按“品牌 车型”汇总年份范围。"""
    years: dict[str, list[int]] = {}
    plain: list[str] = []
    for r in rows:
        if isinstance(r, dict) and r.get("Make") and r.get("Model"):
            key = f"{r['Make']} {r['Model']}"
            y = str(r.get("Year") or "")
            years.setdefault(key, [])
            if y.isdigit():
                years[key].append(int(y))
        else:
            plain.append(_compat_text(r))
    out = [f"{k} {min(v)}-{max(v)}" if v and min(v) != max(v) else (f"{k} {v[0]}" if v else k)
           for k, v in sorted(years.items())]
    return (out + plain)[:COMPAT_SUMMARY_LIMIT]


def _compat_matches(rows: list, vehicle: str) -> list:
    """车型里的每个词（年份、品牌、车型）都要在同一条兼容记录里出现，不区分大小写。"""
    words = [w for w in re.split(r"[\s,/]+", vehicle.lower()) if w]
    return [r for r in rows if all(w in _compat_text(r).lower() for w in words)]


def get_item_details(ctx: RuntimeContext, src: BusinessSource, args: ItemDetailsArgs) -> Outcome:
    """当前会话商品的详细信息（模型按需请求：买家问到商品本身时才查）。item_id 来自运行上下文。"""
    if not ctx.item_id:
        return "NOT_FOUND", None, None, "会话没有关联商品"
    listing = src.get_listing(ctx.store_id, ctx.item_id)
    if listing is None:
        return "NOT_FOUND", None, None, "商品不属于当前店铺或不存在"
    variations = listing.get("variations") or []
    data = {k: listing.get(k) for k in (
        "item_id", "marketplace_id", "title", "sku", "condition", "category", "price", "item_specifics",
        "handling_time", "shipping", "shipping_options", "ship_to_locations", "return_policy",
        "best_offer_enabled", "listing_status", "location", "description_text",
    ) if listing.get(k) not in (None, [], {}, "")}
    # 库存只给有没有货，不给具体数量（提示词同样要求不透露数量）
    data["variations"] = [{"sku": v.get("sku"), "aspects": v.get("aspects") or {},
                           **({"in_stock": v["quantity_available"] > 0}
                              if isinstance(v.get("quantity_available"), int) else {})} for v in variations]
    excluded = listing.get("excluded_ship_to") or []
    if excluded:
        data["excluded_ship_to"] = excluded[:20] + ([f"…共 {len(excluded)} 个"] if len(excluded) > 20 else [])
    compat = listing.get("compatibility") or []
    data["compatibility_total"] = listing.get("compatibility_total") or len(compat)
    if args.vehicle and args.vehicle.strip():
        matches = _compat_matches(compat, args.vehicle)
        data["compatibility_query"] = args.vehicle.strip()
        data["compatibility_matches"] = matches[:COMPAT_MATCH_LIMIT]
        data["compatibility_match_count"] = len(matches)
    else:
        data["compatibility_summary"] = _compat_summary(compat)
    ref = f"listing:{listing['item_id']}"
    if len(variations) > 1:
        return "AMBIGUOUS", data, ref, "多变体刊登，买家指的是哪个 SKU 需确认"
    return "FOUND", data, ref, None


def get_authorized_order_context(ctx: RuntimeContext, src: BusinessSource, args: OrderRefArgs) -> Outcome:
    orders = src.find_orders(ctx.store_id, ctx.buyer_key)
    if args.order_ref:
        matched = [o for o in orders if o["order_id"] == args.order_ref.strip()]
        if matched:
            orders = matched
    fields = ("order_id", "item_id", "sku", "quantity", "title", "marketplace_id", "created_at", "paid_at",
              "payment_status", "order_status", "total", "ship_by_date", "estimated_delivery", "guaranteed_delivery",
              "shipping_service", "ship_to_country", "has_shipment", "cancel_state", "cancel_requested", "refunds",
              "line_items", "items")
    rows = [{k: o[k] for k in fields if o.get(k) not in (None, [], "")} for o in orders]
    if not rows:
        return "NOT_FOUND", None, None, "当前店铺下该买家没有可关联的订单"
    if len(rows) > 1:
        return "AMBIGUOUS", rows, "orders:" + ",".join(r["order_id"] for r in rows), "该买家有多个订单，需买家确认是哪一单"
    return "FOUND", rows[0], f"order:{rows[0]['order_id']}", None


def get_product_by_sku(ctx: RuntimeContext, src: BusinessSource, args: SkuArgs) -> Outcome:
    sku = args.sku.strip().upper()
    if not SKU_PATTERN.match(sku):
        return "NOT_FOUND", None, None, "SKU 格式无效"
    rows = src.find_products(ctx.store_id, sku)
    if not rows:
        return "NOT_FOUND", None, None, f"当前店铺没有 SKU {sku}"
    if len(rows) > 1:
        return "AMBIGUOUS", None, None, f"SKU {sku} 匹配到多条商品"
    p = rows[0]
    data = {k: p.get(k) for k in ("sku", "title", "attributes", "compatibility", "not_compatible", "listing_status")}
    return "FOUND", data, f"product:{sku}", None


def _buyer_owns(ctx: RuntimeContext, src: BusinessSource, order_id: str) -> bool:
    """订单是否属于当前会话的买家（店铺内的订单也不能跨买家查）。"""
    oid = order_id.strip()
    return any(oid in (o.get("order_id"), o.get("legacy_order_id")) for o in src.find_orders(ctx.store_id, ctx.buyer_key))


def _tickets_for(db, order_id: str, numbers: list[str]) -> list[dict]:
    """这个订单已有的谷仓查件单（每天同步，含客服在谷仓手动建的）。谷仓回复是内部信息，只给要点。"""
    from sqlalchemy import or_, select

    from ..models import LogisticsTicket

    conds = [LogisticsTicket.reference_no == order_id] + ([LogisticsTicket.tracking_no.in_(numbers)] if numbers else [])
    from ..logistics.followup import is_draft

    rows = db.scalars(select(LogisticsTicket).where(or_(*conds)).order_by(LogisticsTicket.id.desc()).limit(10))
    # submitted=false：系统刚保存的草稿，还没在谷仓提交，不能对买家说“已联系承运商”
    return [{"io_code": r.io_code, "type": r.type_name, "status": r.io_status_text, "submitted": not is_draft(r),
             "created": r.added_at, "replied": r.replied_at, "reply": (r.reply or "")[:300] or None} for r in rows]


def get_package_status(ctx: RuntimeContext, src: BusinessSource, args: OrderIdArgs, db=None) -> Outcome:
    """物流：承运商、跟踪号、完整轨迹、谷仓出库单、已有查件单，以及程序判断的物流状况（assessment）。
    预计送达日期一律不给模型（不能向买家承诺送达时间），工作台缓存里仍保留。"""
    if not _buyer_owns(ctx, src, args.order_id):
        return "NOT_FOUND", None, None, "订单不属于当前买家"
    pkgs = src.find_packages(ctx.store_id, args.order_id)
    found = (logistics_lookup(db, ctx.store_id, args.order_id, pkgs) if db is not None
             else {"packages": pkgs, "warehouse": [], "notes": []})
    tracking_on = not get_settings().track17_not_ready_reasons()
    # 谷仓出库单：时间为谷仓系统时间（东八区）；揽收 ascan、妥投 dscan 由承运商回传
    whs = {w["order_code"]: {k: w.get(k) for k in ("order_code", "status_label", "shipping_method", "carrier_name",
                                                    "warehouse_code", "date_shipping", "ascan_time", "dscan_time",
                                                    "abnormal_problem_reason")}
           for w in found["warehouse"]}
    notes = "；".join(found["notes"]) or None
    tickets = _tickets_for(db, args.order_id, [p["tracking_number"] for p in found["packages"] if p.get("tracking_number")]
                           ) if db is not None else []
    if not found["packages"]:
        first = next(iter(whs.values()), None)
        if first and first.get("status_label", "").startswith("待发货"):  # 谷仓还在处理，尚未出库：属正常
            a = {"stage": "NOT_SHIPPED", "stage_label": "仓库处理中，尚未出库", "needs_human": False}
            return "FOUND", {"order_id": args.order_id, "warehouse": first, "assessment": a},                 f"warehouse:{first['order_code']}", notes
        a = assess({"tracking_number": None}, tracking_enabled=tracking_on)
        data = {"order_id": args.order_id, "assessment": a, **({"warehouse": first} if first else {}),
                **({"tickets": tickets} if tickets else {})}
        return "NOT_FOUND", data, None, "查不到这个订单的物流信息（没有跟踪号），需人工核实"
    rows = []
    for p in found["packages"]:
        wh = whs.get(p.get("warehouse_order")) or next(iter(whs.values()), None)
        t = p.get("tracking")
        row = {"order_id": p.get("order_id"), "package_id": p.get("package_id"), "carrier": p.get("carrier"),
               "tracking_number": p.get("tracking_number"), "source": p.get("source"),
               "tracking_status": p.get("tracking_status"), "latest_event": p.get("latest_event"),
               "event_time": p.get("event_time")}
        if t:  # 有 17TRACK 轨迹时，状态和最新动态以轨迹为准；完整节点在 tracking.events（新到旧）
            ev = t.get("latest_event") or {}
            row.update(tracking_status=t.get("status_label"), latest_event=ev.get("description"),
                       event_time=ev.get("time") or row["event_time"])
            row["tracking"] = {k: v for k, v in t.items() if k != "estimated_delivery"}
        if p.get("tracking_note"):
            row["tracking_note"] = p["tracking_note"]
        if wh:
            row["warehouse"] = wh
        row["assessment"] = assess({**p, "warehouse": wh}, tracking_enabled=tracking_on)
        if tickets:
            row["tickets"] = [x for x in tickets if x.get("io_code")]
        rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    if len(rows) > 1:
        return "FOUND", rows, f"packages:{args.order_id}", "该订单有多个包裹" + (f"；{notes}" if notes else "")
    return "FOUND", rows[0], f"package:{rows[0].get('package_id') or rows[0]['tracking_number']}", notes


def get_case_status(ctx: RuntimeContext, src: BusinessSource, args: OrderIdArgs) -> Outcome:
    if not _buyer_owns(ctx, src, args.order_id):
        return "NOT_FOUND", None, None, "订单不属于当前买家"
    cases = src.find_cases(ctx.store_id, [args.order_id])
    if not cases:
        return "NOT_FOUND", [], None, "该订单没有售后 case"
    fields = ("case_type", "case_id", "order_id", "status", "opened_at", "due_by", "last_action")
    rows = [{k: c.get(k) for k in fields} for c in cases]
    return "FOUND", rows, "cases:" + ",".join(r["case_id"] for r in rows), None


def get_inventory(ctx: RuntimeContext, src: BusinessSource, args: SkuArgs) -> Outcome:
    sku = args.sku.strip().upper()
    row = src.get_inventory(ctx.store_id, sku)
    if row is None:
        return "NOT_FOUND", None, None, f"没有 {sku} 的库存记录（不代表售罄）"
    sellable = row.get("sellable_quantity", 0)
    data = {
        "sku": sku,
        # 对买家只能用可售措辞，内部数量仅供程序判断（第 12.2 节）
        "availability": "IN_STOCK" if sellable > 0 else ("INBOUND" if row.get("in_transit_quantity") else "OUT_OF_STOCK"),
        "sellable_quantity": sellable,
        "reserved_quantity": row.get("reserved_quantity"),
        "in_transit_quantity": row.get("in_transit_quantity"),
        "warehouse": row.get("warehouse"),
    }
    return "FOUND", data, f"inventory:{sku}", "数量仅供内部判断，回复只可描述是否有货"
