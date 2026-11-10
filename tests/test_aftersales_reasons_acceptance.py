"""验收（测试工程师，2026-11-10）：订单售后原因分类——补开发用例没覆盖的规则、优先级、人工与 AI 交互、解析边界、
对话 ID 筛选、导出与列表一致、统计加总、侧栏打标后重算。"""
import csv
import io
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_aftersales_reasons import D, _rows, _seed, _sig


# ---------------------------------------------------------------- 规则补充
@pytest.mark.parametrize("sig, want, note_has", [
    (dict(cancels=["OUT_OF_STOCK_OR_CANNOT_FULFILL"]), "LISTING", None),             # 缺货 → listing问题
    (dict(cancels=["BUYER_ASKED_CANCEL"]), "CUSTOMER", None),
    (dict(disputes=["SIGNIFICANTLY_NOT_AS_DESCRIBED"]), "LISTING", None),
    (dict(disputes=["FRAUD"]), None, "付款纠纷原因 FRAUD"),
    (dict(case=True, case_types=["ITEM_NOT_RECEIVED"]), "LOGISTICS", None),
    (dict(case=True, case_types=["RETURN"]), None, "退货升级来的"),
    (dict(case=True), None, "纠纷详情没有拉取"),
    (dict(parcel=["谷仓发错"]), "WAREHOUSE", None),
    (dict(parcel=["地址问题"]), "CUSTOMER", None),
    (dict(returns=["ARRIVED_DAMAGED"], parcel=["谷仓发错"]), "WAREHOUSE", None),
    (dict(returns=["FAKE_OR_COUNTERFEIT"], inquiry=True), "LOGISTICS", None),  # 退货原因判不出：看下一级
    (dict(returns=["FAKE_OR_COUNTERFEIT"]), None, "FAKE_OR_COUNTERFEIT"),
    (dict(cancels=["ORDER_MISTAKE"], feishu=["质量问题"]), "CUSTOMER", None),  # 取消（4）优先于飞书（5）
    (dict(parcel=["断更"], cancels=["ORDER_MISTAKE"]), "LOGISTICS", None),   # 物流（3）优先于取消（4）
    (dict(feishu=["质量问题"], intents=["fitment"], refunded=True), "PRODUCT", None),  # 飞书（5）优先于意图（6）
    (dict(intents=["delivery_delay"], refunded=True), "LOGISTICS", None),     # 记录：只问过物流也会判物流问题
    (dict(intents=["cancel_request"]), "CUSTOMER", None),
    (dict(intents=["shipping_status"], conv_types=["不适配"]), "FITMENT", None),  # 意图判不出：看转售后类型
    (dict(conv_types=["退货"]), None, "只有售后标记"),
])
def test_more_rules(sig, want, note_has):
    from app.aftersales_reasons import classify

    code, note, basis = classify(_sig(**sig))
    assert code == want, (sig, code, basis)
    if note_has:
        assert note_has in note, note


def test_missing_note_parts():
    from app.aftersales_reasons import classify

    _, note, _ = classify(_sig(returns=["NO_REASON"], refunded=True, conv_ids=[5]))
    assert "退货原因为 NO_REASON" in note and "有关联会话，但没有能判断的" in note
    _, note, _ = classify(_sig(cancels=[None]))
    assert "取消原因 -" in note and "没有关联会话" in note


# ---------------------------------------------------------------- 文本解析边界
@pytest.mark.parametrize("text, want", [
    ("物流问题\t延误\n第二行", ("LOGISTICS", "延误 第二行")),
    ("仓库原因-漏放", ("WAREHOUSE", "漏放")),
    ("仓库原因，包装太差", ("WAREHOUSE", "包装太差")),
    ("customer 买错了", ("CUSTOMER", "买错了")),
    ("　客户原因　　买错了　", ("CUSTOMER", "买错了")),
])
def test_parse_more(text, want):
    from app.aftersales_reasons import parse_text

    assert parse_text(text) == want


@pytest.mark.parametrize("text", ["仓库原因（漏放）", "仓库原因(发错货)", "物流 问题 丢件", "未分类 备注"])
def test_parse_rejects_more(text):
    """记录：括号紧跟原因、原因中间有空格、“未分类”都不认（422），页面要提示用“原因 + 空格 + 备注”。"""
    from app.aftersales_reasons import parse_text

    with pytest.raises(ValueError):
        parse_text(text)


# ---------------------------------------------------------------- 人工优先：AI 依据后来变了
def test_ai_changes_under_manual_then_undo(client):
    from app.aftersales_reasons import rebuild_all
    from app.db import session_scope
    from app.models import EbayCase

    _seed()
    rebuild_all()
    rid = _rows()["O7"]["id"]  # 只有退款标记：未分类
    assert client.put(f"/internal/aftersales-reasons/{rid}", json={"text": "客户原因 买家说买错了"}).status_code == 200
    with session_scope() as db:  # 后来同步到这张订单的退货单：产品问题
        db.add(EbayCase(store_id="RS_A", kind="RETURN", external_id="R7", order_id="O7", buyer="gus", reason="DEFECTIVE_ITEM",
                        opened_at=D(2)))
    rebuild_all()
    row = _rows()["O7"]
    assert (row["ai_reason"], row["manual_reason"], row["final_reason"]) == ("PRODUCT", "CUSTOMER", "CUSTOMER")
    r = client.delete(f"/internal/aftersales-reasons/{rid}/manual").json()
    assert (r["final_reason"], r["status"]) == ("PRODUCT", "AI")  # 撤销后回到最新的 AI 结果


def test_sidebar_mark_old_order_survives_rebuild(client):
    """侧栏给 180 天外 / 没售后信号的订单打标：新建一行；之后 AI 重算不删、不改人工。"""
    from app.aftersales_reasons import rebuild_all
    from app.db import session_scope
    from app.models import Conversation

    convs = _seed()
    rebuild_all()
    with session_scope() as db:
        c = Conversation(store_id="RS_A", ebay_conversation_id="C4", buyer_key="dan", item_id="444", conversation_type="FROM_MEMBERS")
        db.add(c)
        db.flush()
        cid = c.id
    r = client.put(f"/internal/conversations/{cid}/aftersales-reason", json={"text": "产品问题 旧单"})
    assert r.status_code == 200 and r.json()["order_id"] == "O4"
    rebuild_all()
    row = _rows()["O4"]
    assert (row["final_reason"], row["manual_reason"]) == ("PRODUCT", "PRODUCT")
    r = client.put(f"/internal/conversations/{convs['C1']}/aftersales-reason", json={"text": "不适配"})  # 同一会话再标：覆盖人工
    assert r.json()["manual_reason"] == "FITMENT"


# ---------------------------------------------------------------- 对话 ID 筛选不误中、导出与列表一致
def test_conversation_filter_exact(client):
    from app.aftersales_reasons import rebuild_all
    from app.db import session_scope
    from app.models import OrderAftersalesReason as R

    _seed()
    rebuild_all()
    with session_scope() as db:
        rows = {r.order_id: r for r in db.scalars(select(R))}
        rows["O1"].conversation_id, rows["O1"].conversation_ids = 11, [11, 21]
        rows["O2"].conversation_id, rows["O2"].conversation_ids = 1, [1]
        rows["O5"].conversation_id, rows["O5"].conversation_ids = 2, [2, 121]
        rows["O6"].conversation_id, rows["O6"].conversation_ids = 30, [30, 12, 1]

    def hit(cid):
        return sorted(x["order_id"] for x in client.get("/internal/aftersales-reasons", params={"conversation_id": cid}).json()["items"])

    assert hit(1) == ["O2", "O6"] and hit(11) == ["O1"] and hit(21) == ["O1"] and hit(12) == ["O6"]
    assert hit(2) == ["O5"] and hit(121) == ["O5"] and hit(3) == []
    assert sorted(x["order_id"] for x in client.get("/internal/aftersales-reasons", params={"q": "21"}).json()["items"]) == ["O1"]


@pytest.mark.parametrize("params", [{}, {"store_id": "RS_A"}, {"reason": "物流问题"}, {"reason": "未分类"}, {"status": "AI"},
                                    {"refunded": "true"}, {"q": "sku-1"}, {"marketplace_id": "EBAY_GB"}])
def test_export_matches_list(client, params):
    from app.aftersales_reasons import rebuild_all

    _seed()
    rebuild_all()
    items = client.get("/internal/aftersales-reasons", params={**params, "page_size": 500}).json()["items"]
    text = client.get("/internal/aftersales-reasons/export", params=params).text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:3] == ["店铺", "订单号", "对话 ID"]
    assert [r[1] for r in rows[1:]] == [x["order_id"] for x in items]
    assert [r[2] for r in rows[1:]] == ["" if x["conversation_id"] is None else str(x["conversation_id"]) for x in items]


def test_export_csv_formula_injection(client):
    from app.aftersales_reasons import rebuild_all

    _seed()
    rebuild_all()
    rid = _rows()["O7"]["id"]
    client.put(f"/internal/aftersales-reasons/{rid}", json={"reason": "产品问题", "note": '=HYPERLINK("http://x.example","点我")'})
    text = client.get("/internal/aftersales-reasons/export").text
    assert ',=HYPERLINK' not in text and ',"=HYPERLINK' not in text


# ---------------------------------------------------------------- 统计加总
def test_stats_sums_consistent(client):
    from app.aftersales_reasons import rebuild_all

    _seed()
    rebuild_all()
    rid = _rows()["O2"]["id"]
    client.put(f"/internal/aftersales-reasons/{rid}", json={"text": "产品问题"})
    s = client.get("/internal/aftersales-reasons/stats").json()
    o = s["overall"]
    assert sum(x["refunded"] for x in s["by_reason"]) == o["refunded"]
    assert abs(sum(x["refund_rate"] or 0 for x in s["by_reason"]) - (o["refund_rate"] or 0)) < 0.001
    assert abs(sum(x["share"] or 0 for x in s["by_reason"]) - 1) < 0.001
    assert sum(x["orders"] for x in s["by_store"]) == o["orders"] and sum(x["refunded"] for x in s["by_store"]) == o["refunded"]
    cur = {m["currency"] for m in o["refund_amount"]}
    assert cur == {"EUR", "GBP"}  # 不同币种分开，不相加
