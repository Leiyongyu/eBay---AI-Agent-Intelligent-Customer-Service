"""议价商品同步：只同步议价店铺（OFFER_LIVE_STORES，现在是 demo-store-02）议价页要用的数据，存进 offer_items，议价页直接读库。

每个店铺每轮：
1. 9 个站点各调一次 Negotiation find_eligible_items（有感兴趣买家的刊登号）：在列表里的标“可议价”、更新最近出现时间，
   不在了的标“不可议价”（保留商品信息，下次又出现时不用重查）；某个站点报错只记下来，不影响其他站点；
2. 可议价、商品信息没取过或超过 DETAIL_MAX_AGE 的，用 Trading GetItem 只取标题、售价、主图、SKU、可售数量、状态
   （EbayDataSource.offer_brief），每轮最多 DETAIL_LIMIT 个；同一个刊登在几个站点都出现时只查一次。
不拉消息、订单、售后等其他数据；全部只读。结果记在 ebay_sync_state（conversation_type=OFFERS），数据同步页能看到。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import offers
from .config import get_settings
from .db import session_scope, utcnow
from .models import EbaySyncState, OfferItem, Store
from .observability import trace_scope

log = logging.getLogger(__name__)

SYNC_TYPE = "OFFERS"
DETAIL_MAX_AGE = timedelta(hours=6)  # 价格、库存会变：可议价的商品 6 小时刷新一次商品信息
DETAIL_LIMIT = 400  # 每个店铺每轮最多查多少个商品信息（GetItem）


def offer_stores(db: Session) -> list[Store]:
    """要同步的店铺：可发报价的店铺（OFFER_LIVE_STORES）里有 eBay 秘钥的。"""
    from .security.store_credentials import ebay_store_rows

    with_keys = {r.store_id for r in ebay_store_rows(db)}
    return [s for s in db.scalars(select(Store).order_by(Store.name)) if s.id in with_keys and offers.offer_enabled(s)]


def _state(db: Session, store_id: str) -> EbaySyncState:
    st = db.scalar(select(EbaySyncState).where(EbaySyncState.store_id == store_id, EbaySyncState.conversation_type == SYNC_TYPE))
    if st is None:
        st = EbaySyncState(store_id=store_id, conversation_type=SYNC_TYPE)
        db.add(st)
        db.flush()
    return st


def save_eligible(db: Session, store_id: str, marketplace_id: str, ids: list[str], now: datetime | None = None) -> dict:
    """一个站点的可议价刊登号入库：在列表里的标可议价，以前可议价、这次不在的标不可议价。返回新增 / 下架数量。"""
    now = now or utcnow()
    rows = {r.listing_id: r for r in db.scalars(select(OfferItem).where(OfferItem.store_id == store_id,
                                                                         OfferItem.marketplace_id == marketplace_id))}
    new = gone = 0
    want = set(ids)
    for lid in ids:
        r = rows.get(lid)
        if r is None:
            r = OfferItem(store_id=store_id, marketplace_id=marketplace_id, listing_id=lid, eligible=True, first_seen_at=now)
            db.add(r)
            rows[lid] = r
            new += 1
        r.eligible, r.last_seen_at = True, now
    for lid, r in rows.items():
        if lid not in want and r.eligible:
            r.eligible = False
            gone += 1
    db.flush()
    return {"eligible": len(want), "new": new, "gone": gone}


def _brief(store_id: str, listing_id: str) -> dict | None:
    from .sources import get_source_for_store

    src = get_source_for_store(store_id)
    fn = getattr(src, "offer_brief", None) or src.get_listing
    return fn(store_id, listing_id)


def apply_detail(rows: list[OfferItem], l: dict | None, now: datetime, error: str | None = None) -> None:
    """把一个刊登的商品信息写到它在各站点的行上。"""
    for r in rows:
        r.detail_synced_at = now
        if error:
            r.detail_error = error[:300]
            continue
        if l is None:
            r.detail_error, r.listing_status = "刊登不存在、已删除或不是本店的", "NOT_FOUND"
            continue
        variations = l.get("variations") or []
        price = l.get("price") or {}
        r.title = (l.get("title") or "")[:300] or None
        r.price_value, r.price_currency = price.get("value"), price.get("currency")
        r.picture_url = (l.get("picture_url") or "")[:500] or None
        r.sku = (l.get("sku") or (variations[0].get("sku") if variations else None) or "")[:100] or None
        r.url = (l.get("url") or "")[:300] or None
        r.quantity_available = offers.listing_quantity(l)
        r.listing_status = (l.get("listing_status") or "")[:32] or None
        r.detail_error = None


def sync_details(db: Session, store_id: str, now: datetime | None = None, limit: int = DETAIL_LIMIT) -> dict:
    """可议价、商品信息没取过或过期的刊登：查商品信息（同一刊登只查一次）。"""
    now = now or utcnow()
    stale = now - DETAIL_MAX_AGE
    rows = list(db.scalars(select(OfferItem).where(OfferItem.store_id == store_id, OfferItem.eligible.is_(True))))
    by_listing: dict[str, list[OfferItem]] = {}
    for r in rows:
        by_listing.setdefault(r.listing_id, []).append(r)
    todo = [lid for lid, rs in by_listing.items() if any(r.detail_synced_at is None or r.detail_synced_at < stale for r in rs)]
    # 没取过的优先
    todo.sort(key=lambda lid: min((r.detail_synced_at or datetime.min) for r in by_listing[lid]))
    done = failed = 0
    for lid in todo[:limit]:
        same = list(db.scalars(select(OfferItem).where(OfferItem.store_id == store_id, OfferItem.listing_id == lid)))
        try:
            apply_detail(same, _brief(store_id, lid), now)
            done += 1
        except Exception as e:  # noqa: BLE001  一个商品失败不影响其他
            apply_detail(same, None, now, error=f"{type(e).__name__}: {str(e)[:200]}")
            failed += 1
    db.flush()
    return {"details": done, "detail_failed": failed, "detail_left": max(0, len(todo) - limit)}


def sync_store(store_id: str) -> dict:
    """同步一个店铺：9 个站点的可议价刊登 + 商品信息。"""
    now = utcnow()
    stats: dict = {"sites": {}, "errors": {}}
    with session_scope() as db:
        _state(db, store_id).last_started_at = now
    for mk in offers.MARKETPLACES:
        try:
            ids = offers.find_eligible(store_id, mk, refresh=True)
        except Exception as e:  # noqa: BLE001  某个站点没开通 / 报错：记下来，其他站点照常
            stats["errors"][mk] = str(e)[:200]
            continue
        with session_scope() as db:
            stats["sites"][mk] = save_eligible(db, store_id, mk, ids, now)
    with session_scope() as db:
        stats.update(sync_details(db, store_id, now))
        st = _state(db, store_id)
        st.last_stats = {**stats, "eligible": sum(v["eligible"] for v in stats["sites"].values())}
        if stats["sites"]:
            st.last_success_at = utcnow()
        st.last_error = ("；".join(f"{k}: {v}" for k, v in stats["errors"].items())[:2000] or None) if stats["errors"] else None
    return stats


def sync_all() -> dict:
    with session_scope() as db:
        stores = [(s.id, s.name) for s in offer_stores(db)]
    out = {"stores": len(stores), "result": {}, "errors": {}}
    for sid, name in stores:
        try:
            out["result"][name] = sync_store(sid)
        except Exception as e:  # noqa: BLE001
            log.exception("议价商品同步失败：%s", name)
            out["errors"][name] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ------------------------------------------------------------------ 定时
class OfferSyncWorker:
    """每 OFFER_SYNC_MINUTES 分钟同步一次议价店铺（0 = 暂停；数据同步页和议价页的“刷新”仍可手动拉）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.last_at: datetime | None = None
        self.running_now = False

    def start(self) -> bool:
        from .ebay.stores import ebay_not_ready_reasons

        if ebay_not_ready_reasons():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="offer-sync", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_now(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        self.running_now = True
        try:
            self.last = sync_all()
        except Exception as e:  # noqa: BLE001
            log.exception("议价商品同步失败")
            self.last = {"error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self.last_at = utcnow()
            self.running_now = False
            self._lock.release()
        return self.last

    def run_in_background(self) -> bool:
        if self.running_now:
            return False
        threading.Thread(target=self.run_now, name="offer-sync-now", daemon=True).start()
        return True

    def _loop(self) -> None:
        self._stop.wait(90)
        while not self._stop.is_set():
            minutes = get_settings().offer_sync_minutes
            if minutes > 0 and (self.last_at is None or utcnow() - self.last_at >= timedelta(minutes=minutes)):
                with trace_scope(new=True):  # 每轮同步一个追踪号
                    self.run_now()
            self._stop.wait(60)


worker = OfferSyncWorker()
