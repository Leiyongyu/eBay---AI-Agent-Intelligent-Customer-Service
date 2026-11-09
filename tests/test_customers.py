"""客户管理：订单解析与去重、客户列表按买家汇总（订单、金额、售后、会话）、黑名单（权限、标红、AI 不起草）。"""
from fastapi.testclient import TestClient

from conftest import login, make_user
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm


def order(oid, buyer, total, cur="EUR", refunds=None, created="2026-11-01T10:00:00.000Z"):
    return {"orderId": oid, "creationDate": created, "lastModifiedDate": created, "orderFulfillmentStatus": "FULFILLED",
            "orderPaymentStatus": "PAID", "buyer": {"username": buyer, "taxAddress": {"countryCode": "DE"}},
            "pricingSummary": {"total": {"value": str(total), "currency": cur}},
            "paymentSummary": {"refunds": refunds or []}, "cancelStatus": {"cancelState": "NONE_REQUESTED"},
            "lineItems": [{"legacyItemId": "110000000001", "title": "Turbo", "quantity": 1}]}


def seed():
    from app import customers, ebay_cases
    from app.db import session_scope
    from test_ebay_cases import RET

    with session_scope() as db:
        rows = [customers.parse_order(o) for o in (order("01-1", "Buyer_X", 50), order("01-2", "buyer_x", 30.5, refunds=[{"amount": 1}],
                                                                                    created="2026-11-03T10:00:00.000Z"),
                                                    order("01-3", "buyer_y", 10))]
        assert customers.save_orders(db, "STORE_A", rows) == 3
        assert customers.save_orders(db, "STORE_A", rows[:1]) == 0  # 再拉一次不重复
        customers.save_orders(db, "STORE_B", [customers.parse_order(order("02-1", "buyer_x", 20, "GBP"))])
        ebay_cases.save(db, "STORE_A", "RETURN", [ebay_cases.parse_return({**RET, "buyerLoginName": "buyer_x"})])


def test_customer_list_aggregates_orders_cases_and_conversation(client):
    seed()
    send_buyer(client, "Where is my parcel?", buyer="buyer_x")
    items = {c["buyer"]: c for c in client.get("/internal/customers").json()["items"]}
    x = items["buyer_x"]  # 用户名不区分大小写合并
    assert (x["orders"], x["refunded_orders"], x["returns"], x["country"]) == (3, 1, 1, "DE")
    assert x["amounts"] == {"EUR": 80.5, "GBP": 20.0} and {s["id"] for s in x["stores"]} == {"STORE_A", "STORE_B"}
    assert x["conversation_id"] and not x["blacklist"]
    assert [c["buyer"] for c in client.get("/internal/customers", params={"q": "_y"}).json()["items"]] == ["buyer_y"]
    a_only = client.get("/internal/customers", params={"store_id": "STORE_A"}).json()["items"]
    assert next(c for c in a_only if c["buyer"] == "buyer_x")["amounts"] == {"EUR": 80.5}


def test_blacklist_scope_flags_and_ai_skip(client):
    from app.worker import worker

    seed()
    # 只负责 STORE_A 的客服：只能加自己店铺的黑名单，不能加“全部店铺”
    make_user("cs_bl", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_bl")
    assert cs.post("/internal/blacklist", json={"buyer": "buyer_x", "reason": "恶意退货"}).status_code == 403
    assert cs.post("/internal/blacklist", json={"buyer": "buyer_x", "store_id": "STORE_B"}).status_code == 404
    e = cs.post("/internal/blacklist", json={"buyer": "Buyer_X", "store_id": "STORE_A", "reason": "恶意退货"}).json()
    assert e["buyer"] == "buyer_x"
    assert cs.post("/internal/blacklist", json={"buyer": "buyer_x", "store_id": "STORE_A"}).status_code == 409

    # 客户列表、售后记录标黑名单
    x = next(c for c in client.get("/internal/customers").json()["items"] if c["buyer"] == "buyer_x")
    assert [b["reason"] for b in x["blacklist"]] == ["恶意退货"]
    assert client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()[0]["blacklisted"]
    assert [c["buyer"] for c in client.get("/internal/customers", params={"blacklisted": "true"}).json()["items"]] == ["buyer_x"]

    # 黑名单买家来信：AI 不起草，直接交人工；会话上标黑名单
    use_fake_llm(draft_json())
    r = send_buyer(client, "Where is my parcel?", buyer="buyer_x")
    worker.run_once()
    d = detail(client, r["conversation_id"])
    assert "BUYER_BLACKLISTED" in d["job"]["decision_reasons"] and d["agent_draft"] is None
    assert client.get(f"/internal/conversations/{r['conversation_id']}").json()["blacklist"]["reason"] == "恶意退货"
    card = next(c for c in client.get("/internal/conversations", params={"group": "all"}).json() if c["id"] == r["conversation_id"])
    assert card["blacklist"]["id"] == e["id"]

    # 别的店铺不受影响；移出后恢复
    assert cs.get("/internal/blacklist").json()[0]["store_name"]
    assert cs.delete(f"/internal/blacklist/{e['id']}").json() == {"removed": e["id"]}
    assert client.get(f"/internal/conversations/{r['conversation_id']}").json()["blacklist"] is None
