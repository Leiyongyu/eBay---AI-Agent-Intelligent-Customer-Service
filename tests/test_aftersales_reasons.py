"""订单售后原因分类：规则映射、文本解析、进表范围、人工优先与撤销、会话侧栏标记、退款率口径、店铺权限、幂等、导出。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import login, make_user

NOW = datetime.utcnow()
D = lambda days: NOW - timedelta(days=days)  # noqa: E731


def _sig(**kw):
    from app.aftersales_reasons import _empty_sig

    return {**_empty_sig(), **kw}


# ---------------------------------------------------------------- 规则
@pytest.mark.parametrize("sig, want", [
    (dict(returns=["DEFECTIVE_ITEM"]), "PRODUCT"),
    (dict(returns=["DOES_NOT_FIT"]), "FITMENT"),
    (dict(returns=["MISSING_PARTS"]), "WAREHOUSE"),
    (dict(returns=["ORDERED_DIFFERENT_ITEM"]), "WAREHOUSE"),
    (dict(returns=["NOT_AS_DESCRIBED"]), "LISTING"),
    (dict(returns=["WRONG_SIZE"]), "FITMENT"),  # 用户 11-10 定：尺寸不对归不适配
    (dict(returns=["ORDERED_WRONG_ITEM"]), "CUSTOMER"),
    (dict(returns=["WITHDRAW_FROM_PURCHASE_CONTRACT"]), "CUSTOMER"),
    (dict(returns=["ARRIVED_DAMAGED"]), "LOGISTICS"),
    (dict(returns=["ARRIVED_DAMAGED"], feishu=["破损错发缺件"]), "WAREHOUSE"),
    (dict(disputes=["ITEM_NOT_RECEIVED"]), "LOGISTICS"),
    (dict(inquiry=True), "LOGISTICS"),
    (dict(parcel=["断更"]), "LOGISTICS"),
    (dict(tickets=["妥投未收到"]), "LOGISTICS"),
    (dict(cancels=["ORDER_MISTAKE"]), "CUSTOMER"),
    (dict(cancels=["WONT_ARRIVE_IN_TIME"]), "LOGISTICS"),
    (dict(feishu=["质量问题"]), "PRODUCT"),
    (dict(feishu=["适配核对"]), "FITMENT"),
    (dict(intents=["quality_issue"]), "PRODUCT"),
    (dict(intents=["fitment"], refunded=True), "FITMENT"),
    (dict(intents=["fitment"]), None),  # 只问适配、没退货退款：不算
    (dict(conv_types=["不适配"]), "FITMENT"),
    (dict(returns=["NO_REASON"], refunded=True), None),
    (dict(returns=["DOES_NOT_FIT"], feishu=["质量问题"], inquiry=True), "FITMENT"),  # 退货原因优先
    (dict(inquiry=True, feishu=["质量问题"]), "LOGISTICS"),
])
def test_rules(sig, want):
    from app.aftersales_reasons import classify

    code, note, basis = classify(_sig(**sig))
    assert code == want, (sig, code, basis)
    assert note
    if want is None:
        assert "缺" in note or "判不出" in note or "没有" in note
    if sig.get("returns") == ["ARRIVED_DAMAGED"] and not sig.get("feishu"):
        assert "需核实是运输还是包装" in note


@pytest.mark.parametrize("text, want", [
    ("产品问题 外壳开裂", ("PRODUCT", "外壳开裂")),
    ("产品问题    多个   空格", ("PRODUCT", "多个 空格")),
    ("产品问题　全角空格", ("PRODUCT", "全角空格")),
    ("  不适配  ", ("FITMENT", None)),
    ("listing问题 图片不对", ("LISTING", "图片不对")),
    ("Listing问题 标题", ("LISTING", "标题")),
    ("仓库原因：漏放螺丝", ("WAREHOUSE", "漏放螺丝")),
    ("LOGISTICS 丢件", ("LOGISTICS", "丢件")),
])
def test_parse_text(text, want):
    from app.aftersales_reasons import parse_text

    assert parse_text(text) == want


@pytest.mark.parametrize("text", ["", "随便写写", "产品问题XXX", "退款 产品问题"])
def test_parse_text_rejects(text):
    from app.aftersales_reasons import parse_text

    with pytest.raises(ValueError):
        parse_text(text)


# ---------------------------------------------------------------- 数据
def _seed():
    from app.db import session_scope
    from app.models import (AftersalesCase, Conversation, EbayCase, EbayOrder, EbayOrderLine, LogisticsTicket, Store)

    with session_scope() as db:
        db.add(Store(id="RS_A", name="店铺A", default_marketplace_id="EBAY_DE"))
        db.add(Store(id="RS_B", name="店铺B", default_marketplace_id="EBAY_GB"))
        db.flush()
        orders = [  # (店铺, 订单号, 买家, 商品, SKU, 下单, 订单标记退款)
            ("RS_A", "O1", "ann", "111", "SKU-1,SKU-2", D(10), False),
            ("RS_A", "O2", "ben", "222", "SKU-1", D(20), False),
            ("RS_A", "O3", "cat", "333", "SKU-1", D(30), False),   # 没有售后信号：不进表，只算分母
            ("RS_A", "O4", "dan", "444", "SKU-3", D(300), True),   # 超过 180 天：不进表
            ("RS_A", "O5", "eve", "555", "SKU-3", D(5), False),    # 会话转售后
            ("RS_A", "O6", "fay", "666", "SKU-2", D(6), False),    # 飞书工单
            ("RS_A", "O7", "gus", "777", "SKU-1", D(7), True),     # 订单标记退款、没有别的信号
            ("RS_A", "O8", "hal", "888", "SKU-4", D(8), False),    # 谷仓查件单
            ("RS_B", "P1", "ivy", "999", "SKU-1", D(9), False),
        ]
        for st, oid, buyer, item, skus, at, ref in orders:
            db.add(EbayOrder(store_id=st, order_id=oid, buyer=buyer, item_id=item, item_ids=item, skus=skus, created_at=at,
                             total=50.0, currency="EUR", refunded=ref))
        db.add(EbayOrderLine(store_id="RS_A", order_id="O2", line_item_id="L2", line_key="222-1002", item_id="222"))
        db.add(EbayCase(store_id="RS_A", kind="RETURN", external_id="R1", order_id="O1", buyer="ann", reason="DOES_NOT_FIT",
                        status="CLOSED", opened_at=D(8)))
        db.add(EbayCase(store_id="RS_A", kind="REFUND", external_id="F1", order_id="O1", buyer="ann", amount=-30.5,
                        currency="EUR", opened_at=D(7)))
        db.add(EbayCase(store_id="RS_A", kind="INQUIRY", external_id="I1", order_id="222-1002", buyer="ben", status="OPEN",
                        opened_at=D(15)))
        db.add(EbayCase(store_id="RS_A", kind="RETURN", external_id="R4", order_id="O4", buyer="dan", reason="DEFECTIVE_ITEM",
                        opened_at=D(290)))
        db.add(EbayCase(store_id="RS_B", kind="RETURN", external_id="RB", order_id="P1", buyer="ivy", reason="DEFECTIVE_ITEM",
                        opened_at=D(8)))
        db.add(EbayCase(store_id="RS_B", kind="REFUND", external_id="FB", order_id="P1", amount=-20, currency="GBP", opened_at=D(7)))
        convs = {}
        for key, buyer, item, st, af in (("C1", "Ann", "111", None, None), ("C5", "eve", "555", "PENDING", "不适配"),
                                         ("C6", "fay", "666", None, None), ("C8", "hal", None, None, None)):
            c = Conversation(store_id="RS_A", ebay_conversation_id=key, buyer_key=buyer, item_id=item, marketplace_id="EBAY_DE",
                             conversation_type="FROM_MEMBERS", aftersales_status=st, aftersales_type=af)
            db.add(c)
            db.flush()
            convs[key] = c.id
        db.add(AftersalesCase(conversation_id=convs["C6"], store_id="RS_A", order_id="O6", case_type="售后", category="质量问题",
                              problem_zh="坏了", original_text="broken", status="SENT"))
        db.add(LogisticsTicket(io_code="CJ1", reference_no="O8", type_name="断更", store_id="RS_A"))
        return convs


def _rows():
    from app.db import session_scope
    from app.models import OrderAftersalesReason

    with session_scope() as db:
        return {r.order_id: {k: getattr(r, k) for k in ("id", "status", "ai_reason", "final_reason", "manual_reason", "refunded",
                                                         "refund_amount", "conversation_id", "conversation_ids", "skus", "ai_note",
                                                         "ai_basis")}
                for r in db.scalars(select(OrderAftersalesReason))}


def test_scope_classification_and_idempotent(app_env):
    from app.aftersales_reasons import rebuild_all

    convs = _seed()
    r = rebuild_all()
    assert not r["errors"]
    rows = _rows()
    assert set(rows) == {"O1", "O2", "O5", "O6", "O7", "O8", "P1"}  # O3 无信号、O4 超过 180 天
    assert rows["O1"]["ai_reason"] == "FITMENT" and rows["O1"]["refunded"] and rows["O1"]["refund_amount"] == 30.5
    assert rows["O1"]["conversation_id"] == convs["C1"] and rows["O1"]["skus"] == "SKU-1+SKU-2"  # 买家大小写不同也对上
    assert rows["O2"]["ai_reason"] == "LOGISTICS" and "INQUIRY" in rows["O2"]["ai_basis"]  # 咨询按 line_key 对到订单
    assert rows["O5"]["ai_reason"] == "FITMENT" and rows["O5"]["conversation_id"] == convs["C5"]
    assert rows["O6"]["ai_reason"] == "PRODUCT" and rows["O6"]["conversation_id"] == convs["C6"]
    assert rows["O7"]["status"] == "UNCLASSIFIED" and rows["O7"]["ai_note"].startswith("只有退款记录，没有退货单")
    assert rows["O8"]["ai_reason"] == "LOGISTICS" and rows["O8"]["conversation_id"] == convs["C8"]  # 买家只有一张订单
    again = rebuild_all()
    assert again.get("created", 0) == 0 and again.get("updated", 0) == 0 and _rows() == rows


def test_manual_priority_and_undo(client):
    from app.aftersales_reasons import rebuild_all

    _seed()
    rebuild_all()
    rid = _rows()["O7"]["id"]
    assert client.put(f"/internal/aftersales-reasons/{rid}", json={"text": "随便"}).status_code == 422
    r = client.put(f"/internal/aftersales-reasons/{rid}", json={"text": "产品问题  外壳开裂"}).json()
    assert (r["status"], r["final_reason"], r["reason_label"], r["manual_note"]) == ("MANUAL", "PRODUCT", "产品问题", "外壳开裂")
    rebuild_all()  # AI 重算不覆盖人工
    assert _rows()["O7"]["final_reason"] == "PRODUCT"
    rid1 = _rows()["O1"]["id"]
    r = client.put(f"/internal/aftersales-reasons/{rid1}", json={"reason": "客户原因", "note": "买家自己买错"}).json()
    assert (r["final_reason"], r["ai_reason"], r["status"]) == ("CUSTOMER", "FITMENT", "MANUAL")
    r = client.delete(f"/internal/aftersales-reasons/{rid1}/manual").json()
    assert (r["final_reason"], r["status"], r["manual_reason"]) == ("FITMENT", "AI", None)
    r = client.delete(f"/internal/aftersales-reasons/{rid}/manual").json()
    assert r["status"] == "UNCLASSIFIED" and r["reason_label"] == "未分类"


def test_mark_from_conversation(client):
    from app.aftersales_reasons import rebuild_all
    from app.db import session_scope
    from app.models import Conversation, EbayOrder

    convs = _seed()
    rebuild_all()
    r = client.put(f"/internal/conversations/{convs['C1']}/aftersales-reason", json={"text": "仓库原因 漏放螺丝"}).json()
    assert r["order_id"] == "O1" and r["final_reason"] == "WAREHOUSE"
    by_conv = client.get("/internal/aftersales-reasons", params={"conversation_id": convs["C1"]}).json()
    assert [x["order_id"] for x in by_conv["items"]] == ["O1"] and by_conv["total"] == 1
    ev = by_conv["items"][0]["events"]
    assert {e["kind"] for e in ev} == {"RETURN", "REFUND"} and all(e["kind_label"] for e in ev)
    assert next(e for e in ev if e["kind"] == "RETURN")["reason_label"] not in (None, "")
    with session_scope() as db:  # 买家有两张订单、会话没关联刊登：要选订单
        db.add(EbayOrder(store_id="RS_A", order_id="O9", buyer="hal", item_id="900", created_at=D(3)))
        c = Conversation(store_id="RS_A", ebay_conversation_id="C9", buyer_key="hal", conversation_type="FROM_MEMBERS")
        db.add(c)
        db.flush()
        c9 = c.id
    bad = client.put(f"/internal/conversations/{c9}/aftersales-reason", json={"text": "不适配"})
    assert bad.status_code == 422 and set(bad.json()["detail"]["orders"]) >= {"O8", "O9"}
    ok = client.put(f"/internal/conversations/{c9}/aftersales-reason", json={"order_id": "O9", "text": "不适配 车型年款不对"}).json()
    assert ok["order_id"] == "O9" and ok["status"] == "MANUAL" and ok["conversation_id"] == c9  # 不在表里：新建
    assert "O9" in _rows()


def test_stats_rate_and_sku(client):
    from app.aftersales_reasons import rebuild_all

    _seed()
    rebuild_all()
    s = client.get("/internal/aftersales-reasons/stats", params={"store_id": "RS_A", "min_orders": 2}).json()
    # 分母：店铺 A 近 180 天全部订单 7 张（O4 超过 180 天不在默认范围）；退款：O1（退款单）、O7（订单标记）
    assert s["overall"]["orders"] == 7 and s["overall"]["refunded"] == 2 and s["overall"]["refund_rate"] == round(2 / 7, 4)
    assert s["overall"]["refund_amount"] == [{"currency": "EUR", "amount": 30.5}]
    reasons = {x["reason"]: x for x in s["by_reason"]}
    assert reasons["FITMENT"]["refunded"] == 1 and reasons["UNCLASSIFIED"]["refunded"] == 1
    skus = {x["sku"]: x for x in s["by_sku"]}
    assert skus["SKU-1"]["orders"] == 4 and skus["SKU-1"]["refunded"] == 2  # O1 的 SKU-1、SKU-2 各算一次
    assert skus["SKU-2"]["orders"] == 2 and skus["SKU-2"]["main_reason"] == "FITMENT"
    assert skus["SKU-4"]["small_sample"] and not skus["SKU-1"]["small_sample"]
    assert s["unclassified"]["refunded"] == 1
    old = client.get("/internal/aftersales-reasons/stats",
                     params={"store_id": "RS_A", "date_from": D(400).date().isoformat()}).json()
    assert old["overall"]["orders"] == 8 and old["overall"]["refunded"] == 3  # O4 订单标记退款、没进表也算分子
    lst = client.get("/internal/aftersales-reasons", params={"q": "sku-2"}).json()
    assert {x["order_id"] for x in lst["items"]} == {"O1", "O6"}
    counts = client.get("/internal/aftersales-reasons", params={"status": "AI", "store_id": "RS_A"}).json()["counts"]
    assert counts == {"all": 6, "UNCLASSIFIED": 1, "AI": 5, "MANUAL": 0}  # 不受 status 影响
    assert client.get("/internal/aftersales-reasons", params={"marketplace_id": "EBAY_GB"}).json()["total"] == 1
    assert client.get("/internal/aftersales-reasons/stats", params={"marketplace_id": "EBAY_GB"}).json()["overall"]["orders"] == 1
    opts = client.get("/internal/aftersales-reasons/options").json()
    assert [x["code"] for x in opts["reasons"]] == ["PRODUCT", "FITMENT", "WAREHOUSE", "LISTING", "CUSTOMER", "LOGISTICS"]
    csv = client.get("/internal/aftersales-reasons/export", params={"store_id": "RS_A"})
    assert csv.status_code == 200 and csv.text.lstrip("﻿").startswith("店铺,订单号,对话 ID") and "O1" in csv.text


def test_store_permissions(client):
    from app.aftersales_reasons import rebuild_all

    convs = _seed()
    rebuild_all()
    make_user("cs_b", "CS", stores=["RS_B"])
    cs = TestClient(client.app)
    login(cs, "cs_b")
    items = cs.get("/internal/aftersales-reasons").json()["items"]
    assert [x["order_id"] for x in items] == ["P1"]
    assert cs.put(f"/internal/aftersales-reasons/{_rows()['O1']['id']}", json={"text": "产品问题"}).status_code == 404
    assert cs.put(f"/internal/conversations/{convs['C1']}/aftersales-reason", json={"text": "产品问题"}).status_code == 404
    s = cs.get("/internal/aftersales-reasons/stats").json()
    assert s["overall"]["orders"] == 1 and [x["store_id"] for x in s["by_store"]] == ["RS_B"]
    assert cs.get("/internal/aftersales-reasons/stats", params={"store_id": "RS_A"}).json()["overall"]["orders"] == 0
    assert cs.post("/internal/aftersales-reasons/rebuild").status_code == 403
    assert client.post("/internal/aftersales-reasons/rebuild").status_code == 200


def test_sync_hook_refreshes_store(app_env, monkeypatch):
    """每小时售后同步每个店铺之后重算这个店铺（不连 eBay：拉取函数换成空的）。"""
    from app import ebay_cases
    from app.security import store_credentials

    _seed()

    class Cred:
        def __init__(self, sid):
            self.store_id, self.ebay_user_id, self.ebay_store_name, self.has_secret = sid, "seller_a", "店铺A", True

    monkeypatch.setattr(store_credentials, "ebay_store_rows", lambda db: [Cred("RS_A")])
    monkeypatch.setattr(ebay_cases, "fetch_store", lambda sid, mk: ({}, {}))
    import app.customers as customers
    import app.case_extras as case_extras

    monkeypatch.setattr(customers, "sync_orders", lambda sid, mk: {"new": 0})
    monkeypatch.setattr(case_extras, "refresh_listings", lambda sid: {"fetched": 0, "pending": 0})
    out = ebay_cases.sync_all()
    assert out["reasons_updated"] >= 6 and "REASONS" not in (out["errors"].get("RS_A") or {})
    assert "O1" in _rows() and "P1" not in _rows()


def test_missing_note_explains_sync_range():
    from app.aftersales_reasons import classify

    since = datetime(2026, 10, 14)
    code, note, _ = classify(_sig(refunded=True, order_at=datetime(2026, 9, 1), return_from=since))
    assert code is None and "早于售后同步范围" in note and "2026-10-14" in note and "没有关联会话" in note
    assert "没有拉到任何退货单" in classify(_sig(refunded=True, return_from=None))[1]
    assert "纠纷详情没有拉取" in classify(_sig(case=True))[1]


def test_conversation_mark_validates_order(client):
    """会话侧栏带的订单号必须是这个店铺的、这个会话买家的订单（2 号联调发现）：否则 422 并给可选订单，不建空行。"""
    from app.aftersales_reasons import rebuild_all

    convs = _seed()
    rebuild_all()
    before = set(_rows())
    for oid in ("NOT-THIS", "O2", "P1"):  # 不存在 / 本店别的买家 / 别的店铺
        r = client.put(f"/internal/conversations/{convs['C1']}/aftersales-reason", json={"order_id": oid, "text": "产品问题"})
        assert r.status_code == 422, oid
        assert r.json()["detail"]["orders"] == ["O1"] and r.json()["detail"]["message"]
    assert set(_rows()) == before
    ok = client.put(f"/internal/conversations/{convs['C1']}/aftersales-reason", json={"order_id": "O1", "text": "产品问题"})
    assert ok.status_code == 200 and ok.json()["order_id"] == "O1"


# ---------------------------------------------------------------- R2 / R3 / R4（2026-11-10 用户定的口径）
def test_feishu_overrides_customer_return_reason():
    """买家自选客户类退货原因，但飞书工单记了质量问题 / 破损：以工单为准，依据里写明冲突（会话 10826 这类）。"""
    from app.aftersales_reasons import classify

    code, note, basis = classify(_sig(returns=["WITHDRAW_FROM_PURCHASE_CONTRACT"], feishu=["质量问题"]))
    assert code == "PRODUCT" and "以飞书工单为准：质量问题" in note and basis[0].startswith("冲突：退货原因：撤销购买")
    assert classify(_sig(returns=["NO_LONGER_NEED_ITEM"], feishu=["破损错发缺件"]))[0] == "WAREHOUSE"
    assert classify(_sig(returns=["DOES_NOT_FIT"], feishu=["质量问题"]))[0] == "FITMENT"  # 非客户原因不改
    assert classify(_sig(returns=["NO_LONGER_NEED_ITEM"], feishu=["适配核对"]))[0] == "CUSTOMER"


def test_ai_note_in_chinese():
    from app.aftersales_reasons import classify

    assert classify(_sig(returns=["DOES_NOT_FIT"]))[1] == "退货原因：不适配（DOES_NOT_FIT）"
    assert classify(_sig(case=True, case_types=["ITEM_NOT_RECEIVED"]))[1] == "买家开了未收到货纠纷"
    assert classify(_sig(intents=["quality_issue"]))[1] == "AI 草稿意图：质量问题"
    assert classify(_sig(cancels=["ORDER_MISTAKE"]))[1] == "取消原因：下错单（ORDER_MISTAKE）"


def test_unclassified_kinds():
    from app.aftersales_reasons import unclassified_kind
    from app.models import OrderAftersalesReason as R

    since = datetime(2026, 8, 12)
    old = R(refunded=True, order_created_at=datetime(2026, 7, 1), events=[{"kind": "REFUND"}])
    new = R(refunded=True, order_created_at=datetime(2026, 10, 1), events=[{"kind": "REFUND"}])
    no_reason = R(refunded=True, order_created_at=datetime(2026, 10, 1), events=[{"kind": "RETURN", "reason": "NO_REASON"}])
    fraud = R(refunded=False, events=[{"kind": "PAYMENT_DISPUTE", "reason": "FRAUD"}])
    assert [unclassified_kind(x, since) for x in (old, new, no_reason, fraud)] == \
        ["missing_return_data", "direct_refund", "missing_return_data", "other"]
    assert unclassified_kind(R(final_reason="PRODUCT"), since) is None


def test_stats_unclassified_split_and_baseline_links(client):
    from app.aftersales_reasons import rebuild_all
    from app.db import session_scope
    from app.models import BaselineConversation, BaselineMessage

    _seed()
    with session_scope() as db:  # 上线前历史会话：ann 的（刊登 111 → O1）、写了订单号的、对不上唯一订单的
        for key, buyer, item, body in (("BC1", "Ann", "111", "hi"), ("BC2", "zed", None, "Order 12-34567-89012?"),
                                       ("BC3", "hal", None, "hello")):
            c = BaselineConversation(store_id="RS_A", ebay_conversation_id=key, buyer_key=buyer, item_id=item)
            db.add(c)
            db.flush()
            db.add(BaselineMessage(store_id="RS_A", conversation_id=c.id, ebay_message_id=f"{key}-1", sender_role="BUYER",
                                   sent_at=D(10), body=body))
    rebuild_all()
    rows = _rows()
    item = client.get("/internal/aftersales-reasons", params={"q": "O1"}).json()["items"][0]
    assert [b["ebay_conversation_id"] for b in item["baseline_conversations"]] == ["BC1"]
    o8 = client.get("/internal/aftersales-reasons", params={"q": "O8"}).json()["items"][0]
    assert [b["ebay_conversation_id"] for b in o8["baseline_conversations"]] == ["BC3"]  # 买家只有一张订单、会话没刊登：接
    linked = {b["ebay_conversation_id"] for o in rows for b in client.get(
        "/internal/aftersales-reasons", params={"q": o}).json()["items"][0]["baseline_conversations"]}
    assert "BC2" not in linked  # 写的订单号不是这个店铺的、买家也没有订单：不接
    s = client.get("/internal/aftersales-reasons/stats", params={"store_id": "RS_A"}).json()["unclassified"]
    assert s["orders"] == s["missing_return_data"]["orders"] + s["direct_refund"]["orders"] + s["other"]["orders"]
    assert s["direct_refund"]["label"] == "直接退款"
    o7 = client.get("/internal/aftersales-reasons", params={"q": "O7"}).json()["items"][0]
    assert o7["unclassified_kind"] == "direct_refund"  # O7 下单晚于店铺最早的退货（O4 的退货），只有退款
    only = client.get("/internal/aftersales-reasons", params={"unclassified_kind": "direct_refund", "store_id": "RS_A"}).json()
    assert [x["order_id"] for x in only["items"]] == ["O7"] and only["total"] == 1
    assert client.get("/internal/aftersales-reasons", params={"unclassified_kind": "other"}).json()["total"] == 0
    assert client.get("/internal/aftersales-reasons", params={"unclassified_kind": "x"}).status_code == 422
    csv = client.get("/internal/aftersales-reasons/export", params={"unclassified_kind": "direct_refund"}).text
    assert "O7" in csv and "O1" not in csv
