"""发货通知：只汇总当天（北京时间）在 eBay 标记发货的订单；按站点语言写订单号、承运商、跟踪号、承运商官网；
有取消/退款的标“不发”；审核后模拟发送到买家已有会话或按订单新建的会话；每单只生成一次。全部用模拟的 eBay 数据。"""
from datetime import date, datetime, timedelta, timezone

DAY = date(2026, 11, 4)
TODAY_UTC = "2026-11-04T02:10:47.000Z"  # 北京时间 11-04 10:10
YESTERDAY_UTC = "2026-11-03T10:00:00.000Z"  # 北京时间 11-03 18:00


class FakeApi:
    def __init__(self, orders, fulfillments):
        self.orders, self.fulfillments, self.calls = orders, fulfillments, []

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/sell/fulfillment/v1/order":
            return {"orders": self.orders, "total": len(self.orders)}
        oid = path.split("/")[-2]
        return {"fulfillments": self.fulfillments.get(oid, [])}


class FakeSource:
    def __init__(self, api):
        self.api = api

    def _api(self, store_id):
        return self.api


def order(oid, buyer, site="EBAY_DE", **extra):
    return {"orderId": oid, "buyer": {"username": buyer}, "fulfillmentHrefs": ["x"],
            "lineItems": [{"listingMarketplaceId": site}], **extra}


def test_carrier_links_and_templates():
    from app.ship_notices import carrier_link, render

    # 只给官网，跟踪号不拼进网址，买家自己在官网输入
    assert carrier_link("DHL Germany", "0034043", "EBAY_DE") == ("DHL", "https://www.dhl.de/")
    assert carrier_link("Royal Mail", "GN1GB", "EBAY_GB") == ("Royal Mail", "https://www.royalmail.com/")
    assert carrier_link("DPD", "155", "EBAY_GB")[1] == "https://www.dpd.co.uk/"
    assert carrier_link("DPD", "155", "EBAY_DE")[1] == "https://www.dpd.com/de/"
    assert carrier_link("Evri", "H05", "EBAY_GB") == ("Evri", "https://www.evri.com/")
    assert carrier_link("Some Local Post", "X 1", None) == ("Some Local Post", "https://www.17track.net/")
    text, zh = render("01-1", [{"carrier": "DHL", "tracking_no": "N1", "url": "https://www.dhl.de/"}], "de")
    assert "Bestellung 01-1" in text and "DHL, Sendungsnummer: N1, Website: https://www.dhl.de/" in text
    assert "跟踪号：N1" in zh and "官网" in zh
    for lang in ("en", "fr", "it", "es"):
        assert "N1" in render("01-1", [{"carrier": "DHL", "tracking_no": "N1", "url": "https://u"}], lang)[0]


def test_collect_today_skip_cancelled_and_dedupe(app_env, monkeypatch):
    from sqlalchemy import select

    from app import ship_notices
    from app.db import session_scope
    from app.models import ShipNotice, Store

    orders = [order("01-00000-00001", "buyer_de"),
              order("01-00000-00002", "buyer_gb", site="EBAY_GB"),
              order("01-00000-00003", "buyer_old"),
              order("01-00000-00004", "buyer_cx", cancelStatus={"cancelState": "CANCEL_REQUESTED", "cancelRequests": [{}]})]
    fulfil = {"01-00000-00001": [{"shippingCarrierCode": "DHL Germany", "shipmentTrackingNumber": "0034", "shippedDate": TODAY_UTC}],
              "01-00000-00002": [{"shippingCarrierCode": "Royal Mail", "shipmentTrackingNumber": "GN1GB", "shippedDate": TODAY_UTC},
                                 {"shippingCarrierCode": "Evri", "shipmentTrackingNumber": "H05", "shippedDate": TODAY_UTC}],
              "01-00000-00003": [{"shippingCarrierCode": "DHL", "shipmentTrackingNumber": "OLD", "shippedDate": YESTERDAY_UTC}],
              "01-00000-00004": [{"shippingCarrierCode": "DHL", "shipmentTrackingNumber": "CX1", "shippedDate": TODAY_UTC}]}
    api = FakeApi(orders, fulfil)
    with session_scope() as db:
        db.add(Store(id="REAL_A", name="Shop A", ebay_user_id="seller_a", default_marketplace_id="EBAY_DE"))
    monkeypatch.setattr(ship_notices, "_ebay_stores", lambda db: [db.get(Store, "REAL_A")])
    r = ship_notices.collect(DAY, source=FakeSource(api))
    assert (r["orders"], r["new"], r["skipped"], r["errors"]) == (3, 3, 1, [])
    with session_scope() as db:
        rows = {n.order_id: n for n in db.scalars(select(ShipNotice))}
        assert set(rows) == {"01-00000-00001", "01-00000-00002", "01-00000-00004"}  # 昨天发货的不算
        de, gb, cx = rows["01-00000-00001"], rows["01-00000-00002"], rows["01-00000-00004"]
        assert (de.language, de.status, de.ship_date) == ("de", "PENDING", "2026-11-04")
        assert "Sendungsnummer: 0034, Website: https://www.dhl.de/" in de.text and "Liefer" not in de.text  # 不写送达日期
        assert gb.language == "en" and len(gb.packages) == 2 and "Royal Mail" in gb.text and "Evri" in gb.text
        assert (cx.status, cx.skip_reason) == ("SKIPPED", "订单有取消申请")
    calls = len(api.calls)
    assert ship_notices.collect(DAY, source=FakeSource(api))["new"] == 0  # 已生成的订单不重复
    assert len(api.calls) == calls + 2  # 只多了列表请求和昨天那单的发货记录


def test_send_review_flow(client, monkeypatch):
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.models import Conversation, Message, ShipNotice

    with session_scope() as db:
        existing = get_or_create_conversation(db, "STORE_A", "C-SN1", "buyer_has_conv", None, "EBAY_DE", None)
        pk = [{"carrier": "DHL", "tracking_no": "N1", "url": "https://u"}]
        a = ShipNotice(store_id="STORE_A", order_id="01-1", buyer_key="buyer_has_conv", marketplace_id="EBAY_DE",
                       ship_date="2026-11-04", packages=pk, language="de", text="Hallo A", text_zh="您好 A")
        b = ShipNotice(store_id="STORE_A", order_id="01-2", buyer_key="buyer_new", marketplace_id="EBAY_GB",
                       ship_date="2026-11-04", packages=pk, language="en", text="Hello B")
        c = ShipNotice(store_id="STORE_A", order_id="01-3", buyer_key="buyer_c", marketplace_id="EBAY_GB",
                       ship_date="2026-11-04", packages=pk, language="en", text="Hello C")
        db.add_all([a, b, c])
        db.flush()
        ids, existing_id = (a.id, b.id, c.id), existing.id
    r = client.get("/internal/ship-notices", params={"day": "2026-11-04"}).json()
    assert r["counts"] == {"PENDING": 3}
    assert {i["order_id"]: i["conversation_id"] for i in r["items"]}["01-1"] == existing_id  # 买家已有会话
    assert client.patch(f"/internal/ship-notices/{ids[0]}", json={"text": "Hallo A (geändert)"}).status_code == 200
    out = client.post("/internal/ship-notices/send-batch", json={"ids": list(ids[:2])}).json()
    assert out == {"sent": 2, "failed": []}
    assert client.post(f"/internal/ship-notices/{ids[0]}/send").status_code == 409  # 不能重复发
    assert client.post(f"/internal/ship-notices/{ids[2]}/skip", json={"reason": "买家已在 eBay 问过"}).json()["status"] == "SKIPPED"
    with session_scope() as db:
        na, nb = db.get(ShipNotice, ids[0]), db.get(ShipNotice, ids[1])
        assert na.conversation_id == existing_id and na.sent_by == "tester"
        m = db.get(Message, na.sent_message_id)
        assert (m.sender_role, m.body, m.zh_translation) == ("SELLER", "Hallo A (geändert)", "您好 A")
        assert m.ebay_message_id.startswith("SIM-OUT-")  # 模拟发送
        conv = db.get(Conversation, nb.conversation_id)
        assert (conv.ebay_conversation_id, conv.buyer_key, conv.channel_source) == ("ORDER-01-2", "buyer_new", "logistics")
    assert client.get("/internal/ship-notices", params={"day": "2026-11-04"}).json()["counts"] == {"SENT": 2, "SKIPPED": 1}
