"""物流模块复核后的修复（2026-11-04）：测试会话不自动提交查件单、查件单跟进超时转人工、17TRACK 不支持的承运商不算断更、
额度保护、延迟发货、等待揽收选“无Ascan”、取件点超时、不算周日、退货物流、按订单商品找会话、发货通知等仓库发出、POD 保留期。"""
from datetime import datetime, timedelta, timezone

from app.config import get_settings


class FakeGC:
    def __init__(self, order_status="D"):
        self.created, self.order_status = [], order_status

    def ticket_types(self, sm_code=None):
        return [{"value": 2, "name": "断更"}, {"value": 3, "name": "无Ascan"}, {"value": 13, "name": "留言"}]

    def create_ticket(self, **kw):
        self.created.append(kw)
        return f"CJFIX{len(self.created)}"

    def orders_by_reference(self, ref):
        return [{"order_code": "G1", "warehouse_code": "DE", "order_status": self.order_status, "ascan_time": None,
                 "items": [{"sku": "JMH-1-1", "quantity": 1, "item_id": "300000000009"}]}]

    def inventory(self, skus):
        return []


def pkg_state(reason="TRACKING_STALLED", tracking=None, order_id="01-11111-11111"):
    pkg = {"order_id": order_id, "carrier": "DHL", "tracking_number": "TN1",
           "warehouse": {"order_code": "G1", "shipping_method": "DEDHL", "warehouse_code": "DE"},
           "tracking": tracking if tracking is not None else {"status": "InTransit", "events": [{"time": "x"}]},
           "assessment": {"stage": "STALLED", "reason_code": reason, "needs_human": True, "days_without_update": 5.0}}
    return {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg}]}


def _settings(monkeypatch, **kw):
    s = get_settings()
    for k, v in {"logistics_ticket_auto_draft": True, "logistics_ticket_auto_submit": True,
                 "logistics_ticket_notify_emails": "a@example.com", "goodcang_app_token": "t", "goodcang_app_key": "k",
                 **kw}.items():
        monkeypatch.setattr(s, k, v)
    return s


def test_test_conversation_only_drafts(app_env, monkeypatch):
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.logistics import followup

    _settings(monkeypatch)
    gc = FakeGC()
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-FX1", "buyer_fx1", None, "EBAY_DE", None)  # 测试会话
        out = followup.after_reply(db, None, conv, pkg_state(), ["TRACKING_STALLED"], client=gc)
    assert gc.created == [] and out["tickets"][0]["io_code"].startswith("DRAFT-")
    assert out["submit_skipped"].startswith("测试会话")
    assert followup.auto_handled(out) == set()  # 没提交：照旧转人工


def test_stale_open_ticket_escalates(app_env, monkeypatch):
    from app.db import session_scope
    from app.logistics import followup
    from app.logistics.assess import CN
    from app.models import LogisticsTicket

    _settings(monkeypatch, logistics_ticket_followup_days=3)
    old = (datetime.now(CN) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (datetime.now(CN) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    with session_scope() as db:
        db.add(LogisticsTicket(io_code="CJOLD", reference_no="01-11111-11111", tracking_no="TN1", io_status=7,
                               io_status_text="已回复待完结", reply="已催派", replied_at=old, type_name="断更"))
        out = followup.draft_tickets(db, FakeGC(), pkg_state(), ["TRACKING_STALLED"], store_id="STORE_A",
                                     buyer_key="b", conversation_id=None)
        assert out[0]["skipped"] == followup.STALE_SKIP and out[0]["age_days"] >= 5
        assert followup.auto_handled({"tickets": out}) == set()  # 超时：不再当成已处理
        db.add(LogisticsTicket(io_code="CJNEW", reference_no="02-22222-22222", tracking_no="TN2", io_status=7,
                               io_status_text="已回复待完结", reply="已催派", replied_at=fresh, type_name="断更"))
        st = pkg_state(order_id="02-22222-22222")
        st["observations"][0]["data"]["tracking_number"] = "TN2"
        out = followup.draft_tickets(db, FakeGC(), st, ["TRACKING_STALLED"], store_id="STORE_A", buyer_key="b",
                                     conversation_id=None)
        assert out[0]["skipped"] == followup.OPEN_SKIP and followup.auto_handled({"tickets": out}) == {"TRACKING_STALLED"}


def test_assess_untracked_pickup_overdue_and_sundays():
    from app.logistics.assess import assess, effective_days

    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # 周二
    s = get_settings().model_copy(update={"logistics_stall_days": 4, "logistics_reship_days": 7,
                                          "logistics_pickup_days": 7, "logistics_skip_sundays": True})

    def run(p):
        return assess({"tracking_number": "N1", **p}, tracking_enabled=True, now=now, settings=s)

    # 17TRACK 不支持（Royal Mail、USPS）：没轨迹不算断更，只给官网；发货满 7 天没妥投才转人工核实
    a = run({"tracking": None, "tracking_unsupported": True, "warehouse": {"ascan_time": "2026-11-07 10:00:00"}})
    assert (a["stage"], a["needs_human"]) == ("UNTRACKED", False)
    a = run({"tracking": None, "tracking_unsupported": True, "warehouse": {"ascan_time": "2026-10-28 10:00:00"}})
    assert (a["stage"], a["reason_code"]) == ("NO_INFO", "TRACKING_NOT_FOUND") and "官网核实" in a["stage_label"]
    assert run({"tracking": None, "tracking_limited": True})["stage"] == "UNTRACKED"  # 额度用完没查
    # 取件点放满 7 天
    pick = {"status": "AvailableForPickup", "events": [{"time_utc": "2026-11-01T10:00:00Z"}]}
    a = run({"tracking": pick})
    assert (a["stage"], a["reason_code"]) == ("PICKUP_OVERDUE", "TRACKING_PICKUP_OVERDUE")
    # 不算周日：周五最后一条轨迹，到周二只算 3 个工作日，不到断更
    assert effective_days(datetime(2026, 11, 6, 12, tzinfo=timezone.utc), now) == 3.0
    assert run({"tracking": {"status": "InTransit", "events": [{"time_utc": "2026-11-06T12:00:00Z"}]}})["stage"] == "OK"
    assert effective_days(datetime(2026, 11, 6, 12, tzinfo=timezone.utc), now, skip_sundays=False) == 4.0


def test_info_received_prefers_no_ascan(app_env, monkeypatch):
    from app.db import session_scope
    from app.logistics import followup

    _settings(monkeypatch)
    st = pkg_state(tracking={"status": "InfoReceived", "events": [{"time": "2026-11-01", "description": "label"}]})
    with session_scope() as db:
        out = followup.draft_tickets(db, FakeGC(), st, ["TRACKING_STALLED"], store_id="STORE_A", buyer_key="b",
                                     conversation_id=None)
    assert out[0]["type"] == "无Ascan"


def test_ship_late_detected():
    from app.agent.nodes import _tracking_problems

    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def state(ship_by, stage="NOT_SHIPPED"):
        return {"observations": [
            {"tool_name": "get_authorized_order_context", "status": "FOUND",
             "data": {"order_id": "01-1", "order_status": "NOT_STARTED", "ship_by_date": ship_by}},
            {"tool_name": "get_package_status", "status": "FOUND",
             "data": {"order_id": "01-1", "assessment": {"stage": stage, "needs_human": False}}}]}

    assert "SHIP_LATE" in _tracking_problems(state(past), "Wo bleibt meine Bestellung?")
    assert "SHIP_LATE" not in _tracking_problems(state(future), "Wo bleibt meine Bestellung?")


def test_return_tracking_and_order_conversation(app_env):
    from app.agent.schemas import RuntimeContext
    from app.aftersales_mail.service import order_conversation
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.tools.readonly import OrderIdArgs, get_case_status

    class Src:
        name = "fake"

        def find_orders(self, store_id, buyer_key):
            return [{"order_id": "01-1"}]

        def find_cases(self, store_id, order_ids):
            return [{"case_type": "RETURN", "case_id": "5332", "order_id": "01-1", "status": "OPEN",
                     "return_tracking": {"carrier": "DHL", "tracking_number": "0034", "delivery_status": "IN_TRANSIT"}}]

    ctx = RuntimeContext(store_id="STORE_A", conversation_id=1, buyer_key="b", marketplace_id="EBAY_DE",
                         item_id=None, job_id=1, run_generation=1)
    status, rows, _, _ = get_case_status(ctx, Src(), OrderIdArgs(order_id="01-1"))
    assert status == "FOUND" and rows[0]["return_tracking"]["carrier_website"] == "https://www.dhl.de/"

    with session_scope() as db:
        a = get_or_create_conversation(db, "STORE_A", "C-OC1", "buyer_multi", "300000000001", "EBAY_DE", None)
        b = get_or_create_conversation(db, "STORE_A", "C-OC2", "buyer_multi", "300000000002", "EBAY_DE", None)
        a.last_message_at, b.last_message_at = datetime(2026, 11, 1), datetime(2026, 11, 3)
        db.flush()
        assert order_conversation(db, "STORE_A", "buyer_multi", "01-9", ["300000000001"]) == a.id  # 同一商品的会话
        assert order_conversation(db, "STORE_A", "buyer_multi", "01-9", ["999"]) == b.id  # 对不上：最近的会话


def test_ship_notice_waits_for_warehouse(app_env, monkeypatch):
    from sqlalchemy import select

    from app import ship_notices
    from app.db import session_scope
    from app.models import ShipNotice, Store

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    orders = [{"orderId": "01-55555-55555", "buyer": {"username": "bw"}, "fulfillmentHrefs": ["x"],
               "lineItems": [{"listingMarketplaceId": "EBAY_DE", "legacyItemId": "300000000009"}]}]
    fulfil = {"01-55555-55555": [{"shippingCarrierCode": "DHL Germany", "shipmentTrackingNumber": "0034", "shippedDate": now_utc}]}

    class Api:
        def get(self, path, params=None):
            if path == "/sell/fulfillment/v1/order":
                return {"orders": orders, "total": 1}
            return {"fulfillments": fulfil[path.split("/")[-2]]}

    monkeypatch.setattr(get_settings(), "ship_notice_wait_for_warehouse", True)
    with session_scope() as db:
        store = Store(id="REAL_W", name="Shop W", ebay_user_id="s", default_marketplace_id="EBAY_DE")
        db.add(store)
        db.flush()
        day = datetime.now(ship_notices.CN).date()
        st = ship_notices.collect_store(db, Api(), store, day, FakeGC(order_status="W"))
        assert (st["new"], st["waiting"]) == (0, 1)  # 仓库还没真正发出：不生成
        st = ship_notices.collect_store(db, Api(), store, day, FakeGC(order_status="D"))
        assert st["new"] == 1
        n = db.scalar(select(ShipNotice).where(ShipNotice.order_id == "01-55555-55555"))
        assert n.item_ids == ["300000000009"]


def test_pod_kept_longer_than_aftersales_files(app_env, monkeypatch):
    from app.aftersales_mail.service import cleanup
    from app.db import session_scope, utcnow
    from app.models import AftersalesFile

    s = get_settings()
    monkeypatch.setattr(s, "aftersales_retention_days", 30)
    monkeypatch.setattr(s, "logistics_pod_retention_days", 180)
    old = utcnow() - timedelta(days=40)
    with session_scope() as db:
        pod = AftersalesFile(store_id="STORE_A", order_id="01-1", source="GOODCANG_TICKET", original_name="POD.jpg",
                             status="STORED", received_at=old)
        mail = AftersalesFile(store_id="STORE_A", order_id="01-1", source="MAIL", original_name="v.mp4",
                              status="STORED", received_at=old)
        db.add_all([pod, mail])
        db.flush()
        cleanup(db)
        assert (pod.status, mail.status) == ("STORED", "PURGED")


def test_track17_quota_savings(app_env, monkeypatch):
    from app.db import session_scope
    from app.logistics import service

    class GC:
        def __init__(self, dscan):
            self.dscan = dscan

        def orders_by_reference(self, ref):
            return [{"order_code": "G9", "tracking_numbers": ["RM123GB"], "carrier_name": "Royal Mail",
                     "dscan_time": self.dscan, "date_shipping": "2026-11-01 10:00:00"}]

    class Track:
        def __init__(self, remain=150):
            self.registered, self.remain = [], remain

        def register(self, items):
            self.registered += items
            return [{"number": i["number"]} for i in items], []

        def track_info(self, items):
            return {}

        def quota(self):
            return {"quota_remain": self.remain}

    s = get_settings().model_copy(update={"track17_enabled": True, "track17_api_key": "k", "goodcang_app_token": "t",
                                          "goodcang_app_key": "k", "track17_wait_seconds": 0, "track17_min_quota": 10})
    with session_scope() as db:
        t = Track()
        out = service.lookup(db, "STORE_A", "01-1", [], settings=s, goodcang=GC("2026-11-02 12:00:00"), track=t)
        assert t.registered == [] and "省额度" in out["packages"][0]["tracking_note"]  # 已妥投：不注册
    service._quota.update(remain=None, at=None)
    with session_scope() as db:
        t = Track(remain=5)
        out = service.lookup(db, "STORE_A", "01-2", [], settings=s, goodcang=GC(None), track=t)
        p = out["packages"][0]
        assert t.registered == [] and p["tracking_limited"] and "剩余额度 5" in p["tracking_note"]
    service._quota.update(remain=None, at=None)
