"""售后管理列表每行的商品信息（SKU、数量、单价、标题、主图）：全部从数据库读，不在打开页面时实时查 eBay。

- SKU / 数量 / 单价 / 订单号：订单同步时存进 ebay_order_lines（每小时随售后同步增量更新）。
  对应方式：咨询、纠纷、评价的订单号是“商品号-交易号”= line_key；退货、退款、取消单是新格式订单号 = order_id；
  都对不上时按“买家 + 商品号”找最近一单；
- 主图 / 标题 / SKU（订单里没有时）：ebay_listing_info，售后同步时补拉新出现的商品（轻量 GetItem），
  已有的超过 LISTING_STALE 重新拉，拉失败的 LISTING_RETRY 后重试；每个店铺每轮最多 LISTING_BUDGET 个，
  待处理的、非评价的、新的优先。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import session_scope, utcnow
from .models import EbayCase, EbayListingInfo, EbayOrderLine

log = logging.getLogger(__name__)

LISTING_STALE = timedelta(days=7)
LISTING_RETRY = timedelta(hours=6)
LISTING_BUDGET = 200
LISTING_THREADS = 4
CHUNK = 500


def _chunks(values, n=CHUNK):
    values = list(values)
    for i in range(0, len(values), n):
        yield values[i:i + n]


# ------------------------------------------------------------------ 刊登信息入库
def listings_to_fetch(db: Session, store_id: str) -> list[str]:
    """这个店铺售后单里要（重新）拉刊登信息的商品号，按优先级排好。"""
    now = utcnow()
    info = {i.item_id: i for i in db.scalars(select(EbayListingInfo).where(EbayListingInfo.store_id == store_id))}
    rows = db.execute(select(EbayCase.item_id, EbayCase.kind, EbayCase.is_open, EbayCase.opened_at)
                      .where(EbayCase.store_id == store_id, EbayCase.item_id.is_not(None))).all()
    rows.sort(key=lambda r: (r.kind == "FEEDBACK", not r.is_open, -(r.opened_at.timestamp() if r.opened_at else 0)))
    out = []
    for item_id in dict.fromkeys(r.item_id for r in rows if r.item_id):
        i = info.get(item_id)
        if i is None or i.fetched_at is None:
            out.append(item_id)
        elif i.error and i.fetched_at < now - LISTING_RETRY:
            out.append(item_id)
        elif i.fetched_at < now - LISTING_STALE:
            out.append(item_id)
    return out


def refresh_listings(store_id: str, budget: int = LISTING_BUDGET) -> dict:
    """补拉 / 更新一个店铺的刊登信息，返回 {fetched, failed, pending}。"""
    from .sources import get_ebay_source

    with session_scope() as db:
        todo = listings_to_fetch(db, store_id)
    batch = todo[:budget]
    if not batch:
        return {"fetched": 0, "failed": 0, "pending": 0}
    src = get_ebay_source()

    def load(item_id: str):
        try:
            return item_id, src.listing_brief(store_id, item_id), None
        except Exception as e:  # noqa: BLE001 — 一个商品拉不到不影响其他
            return item_id, None, f"{type(e).__name__}: {e}"[:300]

    with ThreadPoolExecutor(LISTING_THREADS) as pool:
        results = list(pool.map(load, batch))
    now = utcnow()
    failed = 0
    with session_scope() as db:
        have = {i.item_id: i for i in db.scalars(select(EbayListingInfo).where(EbayListingInfo.store_id == store_id,
                                                                              EbayListingInfo.item_id.in_(batch)))}
        for item_id, data, err in results:
            i = have.get(item_id)
            if i is None:
                i = EbayListingInfo(store_id=store_id, item_id=item_id)
                db.add(i)
            i.fetched_at, i.error = now, err
            if err:
                failed += 1
                continue
            i.found = data is not None
            if data:
                i.title, i.sku, i.picture_url = (data.get("title") or "")[:300] or None, (data.get("sku") or "")[:100] or None, \
                    (data.get("picture_url") or "")[:1000] or None
    return {"fetched": len(batch) - failed, "failed": failed, "pending": len(todo) - len(batch)}


# ------------------------------------------------------------------ 列表读取
def _line_dict(ln: EbayOrderLine) -> dict:
    return {"sku": ln.sku, "quantity": ln.quantity, "title": ln.title, "order_id": ln.order_id,
            "price": {"value": ln.price_value, "currency": ln.price_currency} if ln.price_value else None}


def extras_for(db: Session, cases: list[EbayCase]) -> dict[int, dict]:
    """一批售后单的商品信息（几次批量查询，不调 eBay）。返回 {case.id: extras}，格式同 /extras 接口。"""
    from .feishu.cases import clean_sku

    if not cases:
        return {}
    stores = {c.store_id for c in cases}
    oids = {c.order_id for c in cases if c.order_id}
    by_key: dict[tuple[str, str], EbayOrderLine] = {}
    by_order: dict[tuple[str, str], list[EbayOrderLine]] = defaultdict(list)
    for chunk in _chunks(oids):
        for ln in db.scalars(select(EbayOrderLine).where(
                EbayOrderLine.store_id.in_(stores),
                or_(EbayOrderLine.line_key.in_(chunk), EbayOrderLine.order_id.in_(chunk),
                    EbayOrderLine.legacy_order_id.in_(chunk)))):
            if ln.line_key:
                by_key[(ln.store_id, ln.line_key)] = ln
            by_order[(ln.store_id, ln.order_id)].append(ln)
            if ln.legacy_order_id and ln.legacy_order_id != ln.order_id:
                by_order[(ln.store_id, ln.legacy_order_id)].append(ln)

    def match(c: EbayCase) -> EbayOrderLine | None:
        if not c.order_id:
            return None
        if (c.store_id, c.order_id) in by_key:
            return by_key[(c.store_id, c.order_id)]
        lines = by_order.get((c.store_id, c.order_id)) or []
        return next((ln for ln in lines if c.item_id and ln.item_id == c.item_id), lines[0] if lines else None)

    found = {c.id: match(c) for c in cases}
    # 订单号对不上的：按 买家 + 商品号 找最近一单
    rest = [c for c in cases if found[c.id] is None and c.buyer and c.item_id]
    if rest:
        latest: dict[tuple[str, str, str], EbayOrderLine] = {}
        buyers = {c.buyer.lower() for c in rest}
        for chunk in _chunks({c.item_id for c in rest}):
            for ln in db.scalars(select(EbayOrderLine).where(
                    EbayOrderLine.store_id.in_({c.store_id for c in rest}), EbayOrderLine.item_id.in_(chunk),
                    EbayOrderLine.buyer.in_(buyers)).order_by(EbayOrderLine.created_at.desc())):
                latest.setdefault((ln.store_id, ln.buyer or "", ln.item_id or ""), ln)
        for c in rest:
            found[c.id] = latest.get((c.store_id, c.buyer.lower(), c.item_id))

    listings: dict[tuple[str, str], EbayListingInfo] = {}
    for chunk in _chunks({c.item_id for c in cases if c.item_id}):
        for i in db.scalars(select(EbayListingInfo).where(EbayListingInfo.store_id.in_(stores),
                                                          EbayListingInfo.item_id.in_(chunk))):
            listings[(i.store_id, i.item_id)] = i

    out = {}
    for c in cases:
        ln = found[c.id]
        li = listings.get((c.store_id, c.item_id)) if c.item_id else None
        e = {"sku": None, "stock_sku": None, "quantity": None, "price": None, "title": c.title, "picture_url": None,
             "order_found": False, "order_id": None, "error": None}
        if ln is not None:
            e.update(_line_dict(ln))
            e["title"] = ln.title or c.title
            e["order_found"] = True
        if li is not None:
            e["picture_url"] = li.picture_url
            if not e["sku"]:
                e["sku"] = li.sku
            e["title"] = e["title"] or li.title
        e["stock_sku"] = clean_sku(e["sku"]) if e["sku"] else None
        out[c.id] = e
    return out
