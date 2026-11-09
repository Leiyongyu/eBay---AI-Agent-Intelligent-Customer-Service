"""工作台列表筛选（客户ID 多个与匹配方式、店铺、站点、星标、订单编号、货运单号、SKU、标签、消息内容、负责人、最近回复人、草稿、时间）
与右侧客户资料覆盖全部店铺。"""
from test_pipeline import send_buyer


def ids(client, **params):
    return {i["id"] for i in client.get("/internal/conversations", params={"group": "all", "kind": "buyer", **params}).json()}


def setup(client):
    from app import customers
    from app.db import session_scope
    from app.models import LogisticsTicket
    from test_customers import order

    a = send_buyer(client, "Where is order 01-777-888?", buyer="buyer_us_001")["conversation_id"]
    b = send_buyer(client, "Does it fit my Golf?", buyer="buyer_us_002", item="110000000003")["conversation_id"]
    c = send_buyer(client, "Hello", buyer="zz_buyer_us_001", store="STORE_B", item=None)["conversation_id"]
    with session_scope() as db:
        o = customers.parse_order({**order("01-555-666", "buyer_us_002", 20),
                                   "lineItems": [{"legacyItemId": "110000000003", "sku": "TURBO-123", "quantity": 1}]})
        customers.save_orders(db, "STORE_A", [o])
        customers.save_orders(db, "STORE_B", [customers.parse_order(order("02-1", "buyer_us_002", 15, "GBP"))])
        db.add(LogisticsTicket(io_code="IO-9", reference_no="01-555-666", tracking_no="TRK999", type_name="催派送", io_status=1))
    return a, b, c


def test_filters(client):
    a, b, c = setup(client)
    assert ids(client, buyers="buyer_us_001,buyer_us_002", buyer_match="exact") == {a, b}
    assert ids(client, buyers="buyer_us", buyer_match="prefix") == {a, b}  # zz_buyer… 不是这个开头
    assert ids(client, buyers="us_001", buyer_match="contains") == {a, c}
    assert ids(client, store_ids="STORE_B") == {c}
    assert ids(client, order_no="01-555-666") == {b}  # 本地订单汇总对上买家
    assert ids(client, order_no="01-777-888") == {a}  # 买家消息里提到的订单号
    assert ids(client, tracking_no="TRK999") == {b}  # 查件单 → 订单 → 买家
    assert ids(client, sku="TURBO") == {b}
    assert ids(client, content="Golf") == {b}
    assert ids(client, draft="none") >= {a, b, c} and ids(client, draft="has").isdisjoint({a, b, c})  # AI 还没跑：都没有草稿
    assert ids(client, assignee="unassigned") >= {a, b, c}
    assert ids(client, last_from="2000-01-01", last_to="2000-01-02") == set()
    assert ids(client, first_from="2000-01-01") >= {a, b, c}
    assert client.get("/internal/conversations", params={"last_from": "bad"}).status_code == 422

    # 星标（同事共用）
    assert client.put(f"/internal/conversations/{b}/star", json={"starred": True}).json()["starred"]
    assert ids(client, starred="true") == {b}
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == b)
    assert card["starred"] and "last_replier" in card

    # 最近回复人：人工在工作台发的记提交人（先让 AI 任务跑完，生成中不能人工发送）
    from app.worker import worker
    from test_pipeline import draft_json, use_fake_llm

    use_fake_llm(draft_json(), draft_json(), draft_json())
    for _ in range(3):
        worker.run_once()
    assert client.post(f"/internal/conversations/{a}/send", json={"text": "It is on the way."}).status_code == 200
    assert ids(client, last_replier="tester") == {a}
    opts = client.get("/internal/conversations/filter-options").json()
    assert {s["id"] for s in opts["stores"]} >= {"STORE_A", "STORE_B"} and "eBay 后台" in opts["repliers"]


def test_customer_panel_all_stores(client):
    a, b, c = setup(client)
    data = client.get(f"/internal/conversations/{b}/customer").json()
    all_ = data["all_stores"]
    assert all_["order_count"] == 2 and all_["amounts"] == {"EUR": 20.0, "GBP": 15.0}
    assert {s["store_id"] for s in all_["stores"]} == {"STORE_A", "STORE_B"}  # 全部店铺，不只是这个会话的店铺
    assert all_["orders"][0]["store_name"] and any(o["skus"] == "TURBO-123" for o in all_["orders"])
