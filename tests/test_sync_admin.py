"""数据同步页：只有管理员；四个任务的开关、频率、范围；页面上改开关立即生效（消息定时拉取关着时不拉，立即运行照样拉）；
发货通知只给对接 eBay 的店铺生成。"""
from fastapi.testclient import TestClient

from conftest import login, make_user


def test_sync_status_admin_only_and_settings(client):
    r = client.get("/internal/admin/sync").json()
    assert [t["key"] for t in r["tasks"]] == ["messages", "push", "cases", "best_offers", "ship_notices"]
    msg = r["tasks"][0]
    assert {"ebay_sync_enabled", "ebay_sync_interval_seconds", "ebay_live_stores"} <= set(msg["settings"])
    # 页面上改频率：存系统配置，立即生效
    assert client.put("/internal/admin/settings", json={"values": {"ebay_sync_interval_seconds": 600, "ebay_cases_sync_minutes": 0}}).status_code == 200
    tasks = {t["key"]: t for t in client.get("/internal/admin/sync").json()["tasks"]}
    assert tasks["messages"]["interval"] == "每 600 秒" and tasks["cases"]["interval"] == "已暂停" and not tasks["cases"]["enabled"]

    make_user("cs_sync", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_sync")
    assert cs.get("/internal/admin/sync").status_code == 403
    assert cs.post("/internal/admin/sync/cases/run").status_code == 403
    assert client.post("/internal/admin/sync/nope/run").status_code == 404


def test_scheduler_respects_switch_and_force(client, monkeypatch):
    from app import config
    from app.ebay import scheduler as sch

    calls = []

    class FakeSync:
        def run(self):
            calls.append(1)
            return {"ok": True, "stores": {}}

    monkeypatch.setattr(sch, "EbayMessageSync", FakeSync)
    s = sch.EbaySyncScheduler()
    monkeypatch.setenv("EBAY_SYNC_ENABLED", "false")
    config.get_settings.cache_clear()
    assert s.run_once() is None and calls == []  # 关着：定时那一轮不拉
    assert s.run_once(force=True)["ok"] and calls == [1]  # 立即运行：照样拉
    assert s.last_at is not None


def test_ship_notices_only_for_live_stores(client, monkeypatch):
    from app import config, ship_notices
    from app.db import session_scope

    monkeypatch.setattr("app.ebay.stores.is_ebay_store", lambda store_id: True)
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    config.get_settings.cache_clear()
    with session_scope() as db:
        ids = {s.id for s in ship_notices._ebay_stores(db)}
    assert ids <= {"STORE_A"}
    monkeypatch.setenv("SHIP_NOTICE_LIVE_ONLY", "false")
    config.get_settings.cache_clear()
    with session_scope() as db:
        all_ids = {s.id for s in ship_notices._ebay_stores(db)}
    assert all_ids >= ids


def test_data_source_catalog_and_call_counts(client, monkeypatch):
    """按功能模块列出 eBay 接口、参数、配置（参数带当前配置值）；统一入口调用会被计数，带数字的路径段合并成 {id}。"""
    import httpx

    from app.ebay import usage_meter
    from app.sources.ebay import EbayReadOnlyApi

    usage_meter.reset()
    monkeypatch.setenv("EBAY_SYNC_LOOKBACK_DAYS", "12")
    from app import config

    config.get_settings.cache_clear()

    class Tok:
        def access_token(self):
            return "t"

        def invalidate(self):
            pass

    def handler(request):
        return httpx.Response(200 if "shipping" in request.url.path else 500, json={})

    api = EbayReadOnlyApi("STORE_A", http=httpx.Client(transport=httpx.MockTransport(handler)), tokens=Tok())
    api.get("/sell/fulfillment/v1/order/01-123-456/shipping_fulfillment")
    try:
        api.get("/sell/fulfillment/v1/order")
    except Exception:
        pass
    r = client.get("/internal/admin/sync").json()
    mods = {m["key"]: m for m in r["modules"]}
    assert {"messages", "push", "send", "cases", "orders", "ship_notices", "panel", "listing", "invoice", "offers"} <= set(mods)
    assert "往回 12 天" in mods["messages"]["apis"][0]["params"]  # 参数带当前配置值
    cfg = {c["key"]: c for c in mods["messages"]["config"]}
    assert cfg["ebay_sync_enabled"]["editable"] and cfg["ebay_sync_stores"]["editable"]
    send_cfg = {c["key"]: c for c in mods["send"]["config"]}
    assert not send_cfg["ebay_channel"]["editable"]  # 不在系统配置里的只读（只能改 .env）
    assert mods["push"]["config"][2]["value"] in ("已设置", "未设置")  # 校验令牌不显示原值
    calls = {c["key"]: c for c in r["calls"]}
    ship = calls["GET /sell/fulfillment/v1/order/{id}/shipping_fulfillment"]
    assert ship["today"] == 1 and ship["today_fail"] == 0
    assert calls["GET /sell/fulfillment/v1/order"]["today_fail"] == 1
    panel_api = next(a for a in mods["panel"]["apis"] if a["key"] == "GET /sell/fulfillment/v1/order/{id}/shipping_fulfillment")
    assert panel_api["calls"]["today"] == 1


def test_pull_scope_star_all_stores_send_stays_live(client, monkeypatch):
    """EBAY_SYNC_STORES=* 拉全部店铺；留空只拉对接店铺；发送范围只看 EBAY_LIVE_STORES。"""
    from types import SimpleNamespace

    from app import config
    from app.db import session_scope
    from app.ebay import stores
    from app.models import Store
    from app.sending.channel import is_live_store

    rows = [SimpleNamespace(store_id="STORE_A", ebay_user_id="seller_a", ebay_store_name=None),
            SimpleNamespace(store_id="STORE_B", ebay_user_id="seller_b", ebay_store_name=None)]
    monkeypatch.setattr("app.security.store_credentials.ebay_store_rows", lambda db: rows)
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    monkeypatch.delenv("EBAY_SYNC_STORES", raising=False)
    config.get_settings.cache_clear()
    assert [t.store_id for t in stores.ebay_store_targets()] == ["STORE_A"]
    monkeypatch.setenv("EBAY_SYNC_STORES", "*")
    config.get_settings.cache_clear()
    assert [t.store_id for t in stores.ebay_store_targets()] == ["STORE_A", "STORE_B"]
    with session_scope() as db:
        assert is_live_store(db.get(Store, "STORE_A")) and not is_live_store(db.get(Store, "STORE_B"))
