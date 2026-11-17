"""会话右侧相关信息的补充：售后（按订单号和 商品号-交易号 / 买家+商品对上）、评价、物流（同 AI 查物流）、查件单、黑名单、内部备注（只能删自己的）。"""
from fastapi.testclient import TestClient

from conftest import login, make_user
from test_pipeline import send_buyer


def test_order_extras_and_notes(client):
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.models import LogisticsTicket
    from test_ebay_cases import INQ, RET

    cid = send_buyer(client, "Where is my parcel?", buyer="buyer_us_001")["conversation_id"]
    with session_scope() as db:
        ec.save(db, "STORE_A", "RETURN", [ec.parse_return({**RET, "orderId": "26-10001-US", "buyerLoginName": "buyer_us_001"})])
        ec.save(db, "STORE_A", "INQUIRY", [ec.parse_inquiry({**INQ, "buyer": "buyer_us_001"})])  # 商品号-交易号，按买家+商品对上
        ec.save(db, "STORE_A", "RETURN", [ec.parse_return({**RET, "returnId": "OTHER", "orderId": "99-999"})])  # 别的订单
        ec.save(db, "STORE_A", "FEEDBACK", [{"external_id": "F9", "order_id": "110000000001-999", "item_id": "110000000001",
                                            "buyer": "buyer_us_001", "status": "Negative", "rating": "Negative", "is_open": True,
                                            "comment": "Slow"}])
        db.add(LogisticsTicket(io_code="IO-1", reference_no="26-10001-US", tracking_no="9400100000000000010001",
                               type_name="催派送", io_status=1))
    params = {"order_id": "26-10001-US", "legacy_order_id": "110000000001-999", "item_ids": "110000000001"}
    r = client.get(f"/internal/conversations/{cid}/order-extras", params=params).json()
    assert sorted(c["kind"] for c in r["cases"]) == ["INQUIRY", "RETURN"]
    assert [f["rating"] for f in r["feedback"]] == ["Negative"]
    assert [t["io_code"] for t in r["tickets"]] == ["IO-1"]
    pkg = r["logistics"]["packages"][0]  # 演示数据：和 AI 查物流同一套
    assert pkg["tracking_number"] == "9400100000000000010001" and pkg["stage_label"]
    assert r["blacklist"] is None and r["notes"] == []

    # 备注：订单备注、买家备注；只能删自己写的
    n1 = client.post(f"/internal/conversations/{cid}/order-notes", json={"text": "买家要求周五前到", "order_id": "26-10001-US"}).json()
    client.post(f"/internal/conversations/{cid}/order-notes", json={"text": "老客户，态度好"})
    notes = client.get(f"/internal/conversations/{cid}/order-extras", params={**params, "logistics": "false"}).json()["notes"]
    assert [n["text"] for n in notes] == ["老客户，态度好", "买家要求周五前到"]
    other = client.get(f"/internal/conversations/{cid}/order-extras", params={"order_id": "26-99999-US", "logistics": "false"}).json()
    assert [n["text"] for n in other["notes"]] == ["老客户，态度好"]  # 买家备注每单都看得到，订单备注只在那一单
    make_user("cs_note", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_note")
    assert cs.delete(f"/internal/order-notes/{n1['id']}").status_code == 403
    assert client.delete(f"/internal/order-notes/{n1['id']}").json() == {"removed": n1["id"]}

    # 黑名单在补充信息里显示
    client.post("/internal/blacklist", json={"buyer": "buyer_us_001", "store_id": "STORE_A", "reason": "恶意退货"})
    assert client.get(f"/internal/conversations/{cid}/order-extras", params={**params, "logistics": "false"}).json()["blacklist"]["reason"] == "恶意退货"


def test_buyer_cases_labels_and_goodcang_consignee(client, monkeypatch):
    """没订单时列出买家在本店的全部售后；售后带中文名；eBay 收件信息不全时从谷仓出库单补（*** 当作没有）。"""
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.logistics import goodcang
    from test_ebay_cases import RET

    cid = send_buyer(client, "Does it fit?", buyer="buyer_us_001")["conversation_id"]
    with session_scope() as db:
        ec.save(db, "STORE_A", "RETURN", [ec.parse_return({**RET, "orderId": "26-77777-US", "buyerLoginName": "buyer_us_001"})])
    r = client.get(f"/internal/conversations/{cid}/order-extras", params={"buyer_cases": "true", "logistics": "false"}).json()
    c = r["cases"][0]
    assert c["status_label"] and c["status_label"] != c["status"] and c["reason_label"] != c["reason"]
    assert r["order_url"] is None and r["consignee"] is None

    row = {"reference_no": "26-77777-US", "order_code": "G1-1", "consignee_name": "Jim", "consignee_last_name": "Mc",
           "consignee_address1": "Main St", "consignee_doorplate": "5", "consignee_city": "Dayton", "consignee_state": "NJ",
           "consignee_zipcode": "18810", "consignee_country_code": "US", "consignee_country_name": "美国",
           "consignee_phone": "***", "consignee_email": "a@b.c"}
    calls = []
    monkeypatch.setattr(goodcang.GoodcangClient, "_post", lambda self, path, body: calls.append(body) or {"ask": "Success", "data": [row]})
    goodcang.clear_cache()
    r = client.get(f"/internal/conversations/{cid}/order-extras",
                   params={"order_id": "26-77777-US", "need_consignee": "true", "logistics": "false"}).json()
    assert r["consignee"] == {"name": "Jim Mc", "company": None, "address": "Main St 5, Dayton, NJ, 18810", "country": "US",
                              "country_name": "美国", "phone": None, "email": "a@b.c", "warehouse_order": "G1-1"}
    assert r["order_url"].endswith("orderid=26-77777-US")
    client.get(f"/internal/conversations/{cid}/order-extras", params={"order_id": "26-77777-US", "need_consignee": "true", "logistics": "false"})
    assert len(calls) == 1  # 10 分钟内同一单不重复查谷仓
    goodcang.GoodcangClient().orders_by_reference("26-77777-US")
    assert len(calls) == 1  # 物流查询和收件信息共用同一份缓存


def test_cancel_without_buyer_matched_by_order_line(client):
    """取消单没有买家名、记的是“商品号-交易号”：多件商品的订单（旧订单号对不上）按订单行对上；售前（没选订单）时按这个买家的订单行对上。"""
    from app.db import session_scope
    from app.models import EbayCase, EbayOrderLine

    cid = send_buyer(client, "Ich habe storniert", buyer="buyer_us_001")["conversation_id"]
    with session_scope() as db:
        for n, (item, txn) in enumerate((("110000000001", "501"), ("110000000002", "502"))):
            db.add(EbayOrderLine(store_id="STORE_A", order_id="27-20002-US", legacy_order_id="27-20002-US",
                                 line_item_id=f"L{n}", line_key=f"{item}-{txn}", item_id=item, buyer="buyer_us_001"))
        db.add(EbayCase(store_id="STORE_A", kind="CANCEL", external_id="C-77", order_id="110000000002-502",
                        item_id="110000000002", buyer=None, status="CLOSED/CANCEL_CLOSED_WITH_REFUND", is_open=False))
        db.add(EbayCase(store_id="STORE_B", kind="CANCEL", external_id="C-OTHER", order_id="110000000002-502",
                        buyer=None, status="CLOSED", is_open=False))  # 别的店铺：不显示
    by_order = client.get(f"/internal/conversations/{cid}/order-extras",
                          params={"order_id": "27-20002-US", "legacy_order_id": "27-20002-US", "logistics": "false"}).json()
    assert [c["external_id"] for c in by_order["cases"]] == ["C-77"]
    presale = client.get(f"/internal/conversations/{cid}/order-extras", params={"buyer_cases": "true", "logistics": "false"}).json()
    assert [c["external_id"] for c in presale["cases"]] == ["C-77"]
