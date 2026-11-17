"""议价商品同步（offer_sync）：只同步可发报价的店铺；9 个站点的可议价刊登入库、不在了标不可议价；商品信息同一刊登只查一次、
过期才刷新；某个站点报错不影响其他；议价页读库（带商品信息），“重新查询”实时查并写回。不连真实 eBay。"""
from datetime import timedelta

import httpx
import pytest


@pytest.fixture()
def fake(monkeypatch):
    from app import offer_sync, offers
    from app.config import get_settings

    state = {"sites": {"EBAY_DE": ["111", "222"], "EBAY_GB": ["222"]}, "briefs": [], "fail_site": None}

    def handler(req: httpx.Request) -> httpx.Response:
        mk = req.headers["X-EBAY-C-MARKETPLACE-ID"]
        if mk == state["fail_site"]:
            return httpx.Response(403, json={"errors": [{"errorId": 1100, "message": "Access denied"}]})
        ids = state["sites"].get(mk) or []
        if not ids:
            return httpx.Response(204)
        return httpx.Response(200, json={"eligibleItems": [{"listingId": i} for i in ids], "total": len(ids)})

    monkeypatch.setattr(offers, "http_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(offers, "_headers", lambda store_id, mk: {"X-EBAY-C-MARKETPLACE-ID": mk})
    offers._cache.clear()

    def brief(store_id, lid):
        state["briefs"].append(lid)
        if lid == "999":
            return None
        return {"title": f"Turbo {lid}", "price": {"value": "120.00", "currency": "EUR"}, "picture_url": f"https://i/{lid}.jpg",
                "sku": f"SKU-{lid}", "url": f"https://www.ebay.de/itm/{lid}", "listing_status": "Active",
                "variations": [{"sku": f"SKU-{lid}", "quantity_available": 5}], "quantity_available": 5}

    monkeypatch.setattr(offer_sync, "_brief", brief)
    s = get_settings()
    monkeypatch.setattr(s, "offer_live_stores", "store_a")
    return state


def _rows():
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import OfferItem

    with session_scope() as db:
        return {(r.marketplace_id, r.listing_id): (r.eligible, r.title, r.quantity_available, r.detail_synced_at is not None)
                for r in db.scalars(select(OfferItem))}


def test_sync_store_eligible_details_and_page_reads_db(client, fake):
    from app import offer_sync

    st = offer_sync.sync_store("STORE_A")
    assert st["sites"]["EBAY_DE"] == {"eligible": 2, "new": 2, "gone": 0} and st["sites"]["EBAY_US"]["eligible"] == 0
    assert sorted(fake["briefs"]) == ["111", "222"]  # 222 在两个站点：只查一次
    rows = _rows()
    assert rows[("EBAY_DE", "111")] == (True, "Turbo 111", 5, True) and rows[("EBAY_GB", "222")][1] == "Turbo 222"

    # 议价页读库：带商品信息，不再调 eBay
    fake["sites"]["EBAY_DE"] = ["111", "222", "333"]  # eBay 上变了，但页面读的是库
    r = client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE"}).json()
    assert r["source"] == "db" and [i["listing_id"] for i in r["items"]] == ["111", "222"] and r["synced_at"]
    assert r["items"][0]["listing"]["title"] == "Turbo 111" and r["items"][0]["listing"]["price"]["value"] == "120.00"
    li = client.get("/internal/offers/listing", params={"store_id": "STORE_A", "listing_id": "111"}).json()
    assert li["title"] == "Turbo 111" and li["quantity_available"] == 5
    # “重新查询”：实时查并写回
    r = client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE", "refresh": "true"}).json()
    assert r["source"] == "live" and [i["listing_id"] for i in r["items"]] == ["111", "222", "333"]
    assert r["items"][2]["listing"] is None  # 新出现的还没商品信息：页面按需查

    # 下一轮：111 不在了 → 不可议价；商品信息没过期不重查；某个站点报错不影响其他
    fake["sites"]["EBAY_DE"] = ["222", "333"]
    fake["fail_site"] = "EBAY_GB"
    fake["briefs"].clear()
    st = offer_sync.sync_store("STORE_A")
    assert st["sites"]["EBAY_DE"]["gone"] == 1 and "EBAY_GB" in st["errors"] and "Access denied" in st["errors"]["EBAY_GB"]
    assert fake["briefs"] == ["333"]
    rows = _rows()
    assert rows[("EBAY_DE", "111")][0] is False and rows[("EBAY_GB", "222")][0] is True  # 报错的站点不动

    # 商品信息过期（6 小时）才刷新；刊登没了记原因
    from sqlalchemy import update

    from app.db import session_scope, utcnow
    from app.models import EbaySyncState, OfferItem

    with session_scope() as db:
        db.execute(update(OfferItem).values(detail_synced_at=utcnow() - timedelta(hours=7)))
    fake["sites"]["EBAY_DE"] = ["222", "333", "999"]
    fake["fail_site"] = None
    fake["briefs"].clear()
    offer_sync.sync_store("STORE_A")
    assert sorted(fake["briefs"]) == ["222", "333", "999"]
    with session_scope() as db:
        r999 = db.scalars(select_one("999")).first()
        assert r999.listing_status == "NOT_FOUND" and r999.detail_error
        st = db.query(EbaySyncState).filter(EbaySyncState.conversation_type == "OFFERS").one()
        assert st.last_success_at and st.last_stats["eligible"] >= 3 and st.last_error is None


def select_one(lid):
    from sqlalchemy import select

    from app.models import OfferItem

    return select(OfferItem).where(OfferItem.listing_id == lid)


def test_only_offer_stores_and_sync_page(client, fake, monkeypatch):
    from types import SimpleNamespace

    from app import offer_sync
    from app.db import session_scope

    with monkeypatch.context() as m:  # 两个店铺都有秘钥，只有 STORE_A 可发报价
        m.setattr("app.security.store_credentials.ebay_store_rows",
                  lambda db: [SimpleNamespace(store_id="STORE_A"), SimpleNamespace(store_id="STORE_B")])
        with session_scope() as db:
            assert [s.id for s in offer_sync.offer_stores(db)] == ["STORE_A"]  # 只同步可发报价的店铺
        out = offer_sync.sync_all()
        assert out["stores"] == 1 and not out["errors"]
    page = client.get("/internal/admin/sync").json()
    task = next(t for t in page["tasks"] if t["key"] == "offers")
    assert task["settings"] == ["offer_sync_minutes", "offer_live_stores"] and task["totals"]["eligible"] == 3
    assert client.post("/internal/admin/sync/offers/run").status_code == 200
    assert any(m["key"] == "offers" for m in page["modules"])
