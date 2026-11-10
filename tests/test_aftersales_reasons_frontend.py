"""售后原因页面（views/AftersalesReasonsView.vue、会话侧栏）和后端接口的契约：前端用到的每个接口、参数和字段都在，
改了字段名这里先报错。数据沿用 test_aftersales_reasons 的 _seed。"""
from fastapi.testclient import TestClient

from conftest import login, make_user
from test_aftersales_reasons import D, _seed

ROW_FIELDS = {"id", "store_id", "store_name", "order_id", "conversation_id", "conversation_ids", "buyer_key", "marketplace_id",
              "skus", "order_created_at", "refunded", "refund_amount", "refund_currency", "events", "ai_reason", "ai_note",
              "ai_basis", "manual_reason", "manual_note", "manual_by", "manual_at", "final_reason", "reason_label", "status",
              "status_label"}
EVENT_FIELDS = {"kind", "kind_label", "reason", "reason_label", "at"}


def _front_params(**extra):
    """前端 baseQuery()：日期按下单时间（YYYY-MM-DD）+ 店铺 + 站点。"""
    return {"date_from": D(179).date().isoformat(), "date_to": D(0).date().isoformat(), **extra}


def _setup(client):
    from app.aftersales_reasons import rebuild_all

    convs = _seed()
    rebuild_all()
    return convs


def test_list_stats_fields_used_by_page(client):
    _setup(client)
    r = client.get("/internal/aftersales-reasons", params=_front_params(page=1, page_size=50))
    assert r.status_code == 200, r.text
    d = r.json()
    assert {"items", "counts", "total"} <= set(d) and set(d["counts"]) >= {"all", "UNCLASSIFIED", "AI", "MANUAL"}
    assert d["items"] and all(ROW_FIELDS <= set(i) for i in d["items"])
    assert all(isinstance(i["conversation_ids"], list) for i in d["items"])
    assert all(isinstance(i["ai_basis"], (list, type(None))) for i in d["items"])
    ev = [e for i in d["items"] for e in i["events"]]
    assert ev and all(EVENT_FIELDS <= set(e) for e in ev)
    unc = [i for i in d["items"] if i["status"] == "UNCLASSIFIED"]
    assert unc and all(i["ai_note"] and i["reason_label"] == "未分类" and i["final_reason"] is None for i in unc)
    # 页签数字不受 status 影响；分页
    only = client.get("/internal/aftersales-reasons", params=_front_params(status="UNCLASSIFIED")).json()
    assert only["counts"] == d["counts"] and only["total"] == d["counts"]["UNCLASSIFIED"]
    assert client.get("/internal/aftersales-reasons", params=_front_params(page=1, page_size=1)).json()["total"] == d["total"]
    # 页面上的其他筛选
    for extra in ({"reason": "FITMENT"}, {"refunded": "true"}, {"refunded": "false"}, {"q": "SKU-1"}, {"store_id": "RS_A"},
                  {"marketplace_id": "EBAY_DE"}):
        assert client.get("/internal/aftersales-reasons", params=_front_params(**extra)).status_code == 200, extra

    s = client.get("/internal/aftersales-reasons/stats", params=_front_params(store_id="RS_A")).json()
    assert {"orders", "refunded", "refund_rate", "refund_amount", "aftersales_orders"} <= set(s["overall"])
    assert all({"reason", "label", "aftersales_orders", "refunded", "share", "refund_rate", "refund_amount"} <= set(x)
               for x in s["by_reason"])
    assert all({"sku", "orders", "refunded", "refund_rate", "main_reason_label", "small_sample"} <= set(x) for x in s["by_sku"])
    assert all({"store_id", "store_name", "orders", "refunded", "refund_rate", "refund_amount"} <= set(x) for x in s["by_store"])
    assert {"orders", "refunded"} <= set(s["unclassified"]) and "min_orders" in s and "sku_count" in s
    assert all({"currency", "amount"} <= set(m) for m in s["overall"]["refund_amount"])

    csv = client.get("/internal/aftersales-reasons/export", params=_front_params(status="UNCLASSIFIED"))
    assert csv.status_code == 200 and "csv" in csv.headers["content-type"]


def test_edit_undo_and_conversation_mark(client):
    convs = _setup(client)
    items = client.get("/internal/aftersales-reasons", params=_front_params()).json()["items"]
    row = items[0]
    # 订单表行内编辑：一行文本 / 下拉 + 备注，返回更新后的一行（前端直接替换）
    r = client.put(f"/internal/aftersales-reasons/{row['id']}", json={"text": "产品问题 外壳开裂"}).json()
    assert ROW_FIELDS <= set(r) and (r["status"], r["manual_reason"], r["manual_note"], r["reason_label"]) == ("MANUAL", "PRODUCT", "外壳开裂", "产品问题")
    r = client.put(f"/internal/aftersales-reasons/{row['id']}", json={"reason": "FITMENT", "note": "车型不对"}).json()
    assert r["final_reason"] == "FITMENT" and r["manual_note"] == "车型不对"
    bad = client.put(f"/internal/aftersales-reasons/{row['id']}", json={"text": "随便写写"})
    assert bad.status_code == 422 and isinstance(bad.json()["detail"], str)  # 前端直接显示 detail
    r = client.request("DELETE", f"/internal/aftersales-reasons/{row['id']}/manual").json()
    assert ROW_FIELDS <= set(r) and r["manual_reason"] is None

    # 会话侧栏：按 conversation_id 读出这个会话的订单行，再按订单标记
    cid = convs["C1"]
    mine = client.get("/internal/aftersales-reasons", params={"conversation_id": cid, "page_size": 50}).json()["items"]
    assert mine and all(cid == m["conversation_id"] or cid in m["conversation_ids"] for m in mine)
    r = client.put(f"/internal/conversations/{cid}/aftersales-reason", json={"order_id": mine[0]["order_id"], "text": "仓库原因 漏放螺丝"})
    assert r.status_code == 200, r.text
    assert r.json()["manual_reason"] == "WAREHOUSE" and ROW_FIELDS <= set(r.json())
    # 没带订单号、会话对不上唯一订单时：422，detail = {message, orders}，侧栏把 orders 加进选项让客服选
    nobody = convs["C8"]  # C8 的买家没有对得上的订单
    r = client.put(f"/internal/conversations/{nobody}/aftersales-reason", json={"text": "产品问题"})
    if r.status_code == 422:
        d = r.json()["detail"]
        assert isinstance(d, dict) and d["message"] and isinstance(d["orders"], list)
    else:
        assert r.status_code == 200 and ROW_FIELDS <= set(r.json())


def test_mark_in_unclassified_tab_moves_row_and_counts(client):
    """前端在“未分类”页签标好一行后马上移走这一行，再静默重拉列表：重拉的结果里不能再有它，页签数字跟着变。"""
    _setup(client)
    p = _front_params(status="UNCLASSIFIED", page=1, page_size=50)
    before = client.get("/internal/aftersales-reasons", params=p).json()
    row = before["items"][0]
    r = client.put(f"/internal/aftersales-reasons/{row['id']}", json={"text": "客户原因 买错了"}).json()
    assert r["status"] == "MANUAL"  # 前端据此判断不再属于“未分类”
    after = client.get("/internal/aftersales-reasons", params=p).json()
    assert row["id"] not in {i["id"] for i in after["items"]}
    assert after["counts"]["UNCLASSIFIED"] == before["counts"]["UNCLASSIFIED"] - 1
    assert after["counts"]["MANUAL"] == before["counts"]["MANUAL"] + 1 and after["counts"]["all"] == before["counts"]["all"]
    s = client.get("/internal/aftersales-reasons/stats", params=_front_params()).json()
    assert s["unclassified"]["orders"] == after["counts"]["UNCLASSIFIED"]
    # 未分类再分：前端读 unclassified.<kind>.{label, orders, refunded}、每行的 unclassified_kind；几类加起来等于总数
    kinds = ("missing_return_data", "direct_refund", "other")
    assert all({"label", "orders", "refunded"} <= set(s["unclassified"][k]) for k in kinds)
    assert sum(s["unclassified"][k]["orders"] for k in kinds) == s["unclassified"]["orders"]
    assert all(i["unclassified_kind"] in kinds for i in after["items"])
    assert r["unclassified_kind"] is None
    # 未分类页签的二级筛选：前端同时带 status=UNCLASSIFIED 和 unclassified_kind；数字和 stats 对得上，导出也认这个参数
    for k in kinds:
        one = client.get("/internal/aftersales-reasons", params={**p, "unclassified_kind": k}).json()
        assert one["total"] == s["unclassified"][k]["orders"] and all(i["unclassified_kind"] == k for i in one["items"])
        assert one["counts"] == after["counts"]
        assert client.get("/internal/aftersales-reasons/export", params={**p, "unclassified_kind": k}).status_code == 200
    assert client.get("/internal/aftersales-reasons", params={**p, "unclassified_kind": "x"}).status_code == 422
    # 上线前历史会话：前端只显示 ebay_conversation_id，不能在工作台打开
    assert all(isinstance(i["baseline_conversations"], list) for i in after["items"])
    assert all({"id", "ebay_conversation_id", "first_message_at", "last_message_at"} <= set(b)
               for i in after["items"] for b in i["baseline_conversations"])
    # 撤销后回到“未分类”
    client.request("DELETE", f"/internal/aftersales-reasons/{row['id']}/manual")
    back = client.get("/internal/aftersales-reasons", params=p).json()
    assert back["counts"] == before["counts"]


def test_rebuild_admin_only_and_options(client):
    _setup(client)
    r = client.post("/internal/aftersales-reasons/rebuild")
    assert r.status_code == 200 and "updated" in r.json()
    opts = client.get("/internal/aftersales-reasons/options").json()
    assert {x["code"] for x in opts["reasons"]} == {"PRODUCT", "FITMENT", "WAREHOUSE", "LISTING", "CUSTOMER", "LOGISTICS"}
    labels = {x["code"]: x["label"] for x in opts["reasons"]}
    from_front = {"PRODUCT": "产品问题", "FITMENT": "不适配", "WAREHOUSE": "仓库原因", "LISTING": "listing问题",
                  "CUSTOMER": "客户原因", "LOGISTICS": "物流问题"}  # frontend/src/aftersalesReason.ts 的 REASONS
    assert labels == from_front
    make_user("cs_ar", "CS", stores=["RS_A"])
    cs = TestClient(client.app)
    login(cs, "cs_ar")
    assert cs.post("/internal/aftersales-reasons/rebuild").status_code == 403
    assert {i["store_id"] for i in cs.get("/internal/aftersales-reasons", params=_front_params()).json()["items"]} == {"RS_A"}
