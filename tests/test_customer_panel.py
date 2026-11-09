"""会话页右侧：相关信息（订单）、客户资料（累计）、历史记录（同一买家的其他会话）。"""


def _conv(store, key, buyer, item_id):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        c = get_or_create_conversation(db, store, key, buyer, item_id, "EBAY_US", "simulator")
        _append_message(db, c, f"SIM-IN-{key}", "BUYER", "IN", f"Where is my order? ({key})", None)
        db.flush()
        return c.id


def test_customer_panel_orders_profile_history(client):
    cur = _conv("STORE_A", "P1", "buyer_us_001", "110000000001")
    other = _conv("STORE_A", "P2", "BUYER_US_001", "110000000099")  # 买家名大小写不同也算同一人
    _conv("STORE_A", "P3", "someone_else", None)

    r = client.get(f"/internal/conversations/{cur}/customer")
    assert r.status_code == 200
    d = r.json()
    assert d["related_order"]["order_id"] == "26-10001-US"
    assert any(o["order_id"] == "26-10001-US" for o in d["orders"])
    assert d["packages"] and d["packages"][0]["tracking_number"]
    assert d["profile"]["order_count"] >= 1 and d["profile"]["total_quantity"] >= 1
    assert [h["id"] for h in d["history"]] == [other]
    assert "Where is my order" in d["history"][0]["preview"] and d["errors"] == []


def test_customer_panel_ebay_order_view():
    from app.customer_panel import order_view

    o = order_view({
        "orderId": "23-15360-19549", "salesRecordReference": "5240", "creationDate": "2026-11-04T17:05:21Z",
        "orderPaymentStatus": "PAID", "orderFulfillmentStatus": "NOT_STARTED",
        "pricingSummary": {"total": {"value": "120.83", "currency": "GBP"}},
        "paymentSummary": {"payments": [{"paymentDate": "2026-11-04T18:15:30Z"}],
                           "refunds": [{"amount": {"value": "10.00", "currency": "GBP"}}]},
        "lineItems": [{"legacyItemId": "404939987985", "sku": "BMW-30046-0092", "title": "Headlight", "quantity": 1}],
        "buyerCheckoutNotes": "Buyer's Vehicle: BMW 3 Series 2016",
        "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
            "fullName": "Bruno Vitoria", "primaryPhone": {"phoneNumber": "7435080535"},
            "contactAddress": {"addressLine1": "2 Reedsdale Road", "city": "Manchester",
                               "stateOrProvince": "Greater Manchester", "postalCode": "M40 0AN", "countryCode": "GB"}}}}],
    })
    assert o["record_no"] == "5240" and o["total"] == {"value": "120.83", "currency": "GBP"}
    assert o["ship_to"]["address"] == "2 Reedsdale Road, Manchester, Greater Manchester, M40 0AN"
    assert o["ship_to"]["phone"] == "7435080535" and o["buyer_note"].startswith("Buyer's Vehicle")
    assert o["refunds"][0]["amount"]["value"] == "10.00" and o["quantity"] == 1
