"""售后管理页面里 eBay 原样的类型/状态/原因 → 中文。"""
from backend.app import case_labels as L


def test_status_single_and_combo():
    assert L.status_label("OPEN") == "处理中"
    assert L.status_label("CLOSED/ESCALATED") == "已关闭（曾升级到 eBay）"
    assert L.status_label("ITEM_READY_TO_SHIP/READY_FOR_SHIPPING") == "等待买家寄回"
    # 已关闭/具体结果 → 只说结果
    assert L.status_label("CLOSED/CANCEL_CLOSED_WITH_REFUND") == "已取消并退款"
    # 两段意思一样只显示一个
    assert L.status_label("REFUND_ISSUED/REFUND_SENT") == "已退款"
    # 两段不同用 · 连接
    assert L.status_label("ITEM_SHIPPED/WAITING_FOR_RMA") == "买家已寄回 · 等待退货授权号"
    assert L.status_label("Positive") == "好评"


def test_unknown_kept_as_is():
    assert L.status_label("SOMETHING_NEW") == "SOMETHING_NEW"
    assert L.reason_label("BRAND_NEW_REASON") == "BRAND_NEW_REASON"
    assert L.status_label(None) is None and L.reason_label("") == ""


def test_reason_and_detail():
    assert L.reason_label("NOT_AS_DESCRIBED") == "与描述不符"
    assert L.reason_label("BUYER_ASKED_CANCEL") == "买家要求取消"
    rows = L.detail_labels({"type": "MONEY_BACK", "seller_action": "SELLER_ISSUE_REFUND", "responded": True,
                            "action_url": "https://www.ebay.de/x", "empty": None})
    got = {r["key"]: (r["key_label"], r["value_label"]) for r in rows}
    assert got["type"] == ("退货类型", "退款")
    assert got["seller_action"] == ("卖家需要做", "给买家退款")
    assert got["responded"] == ("已回复", "是")
    assert got["action_url"][1] == "https://www.ebay.de/x"
    assert "empty" not in got
