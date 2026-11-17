"""退款单页面：退款单类型（主动退款 / 纠纷退货 / 取消交易）、当地币种金额、全额 / 部分、退款原因、纠纷编号、SKU，
以及对应的筛选、二级页签数量和原因选项。全部读库，不调 eBay。"""
from datetime import datetime


def _seed():
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.models import EbayOrder, EbayOrderLine

    t = datetime(2026, 11, 10, 8, 0)
    with session_scope() as db:
        for oid, total, cur, sku in (("A-1", 31.93, "EUR", "MCD-1"), ("B-1", 100.0, "GBP", "RNG-1"), ("C-1", 50.0, "EUR", "DAS-1"),
                                     ("D-1", 20.0, "USD", "BMW-1")):
            db.add(EbayOrder(store_id="STORE_A", order_id=oid, buyer="b_" + oid.lower(), total=total, currency=cur, skus=sku,
                             refunded=True, cancelled=False, created_at=t))
            db.add(EbayOrderLine(store_id="STORE_A", order_id=oid, line_item_id="9" + oid[0], line_key=f"11{oid[0]}-9{oid[0]}",
                                 item_id="11" + oid[0], sku=sku))
        # 退货（B）、取消（C）、纠纷（D，纠纷的订单号是 商品号-交易号）
        ec.save(db, "STORE_A", "RETURN", [{"external_id": "5333110234", "order_id": "B-1", "buyer": "b_b-1", "status": "CLOSED",
                                           "amount": 100.0, "currency": "GBP", "reason": "ORDERED_WRONG_ITEM", "is_open": False,
                                           "opened_at": t, "detail": {"action_url": "https://www.ebay.co.uk/rt/ReturnDetails?returnId=5333110234"}}])
        ec.save(db, "STORE_A", "CANCEL", [{"external_id": "7001", "order_id": "C-1", "buyer": "b_c-1", "status": "CANCEL_CLOSED_WITH_REFUND",
                                           "amount": 20.0, "currency": "EUR", "reason": "ORDER_MISTAKE", "is_open": False, "opened_at": t}])
        ec.save(db, "STORE_A", "CASE", [{"external_id": "5099", "order_id": "11D-9D", "buyer": "b_d-1", "status": "CLOSED",
                                         "reason": "ITEM_NOT_RECEIVED", "is_open": False, "opened_at": t}])
        refunds = [
            # 主动退款：eBay 给了换算前金额，等于订单总额 → 全额
            ec.parse_refund({"transactionId": "RRP-A", "orderId": "A-1", "buyer": {"username": "b_a-1"}, "transactionStatus": "PAYOUT",
                             "transactionDate": "2026-11-11T03:08:10.058Z", "bookingEntry": "DEBIT", "transactionMemo": "Refund",
                             "amount": {"value": "29.30", "currency": "USD", "convertedFromValue": "27.13", "convertedFromCurrency": "EUR",
                                        "exchangeRate": "1.08"},
                             "totalFeeBasisAmount": {"value": "34.48", "currency": "USD", "convertedFromValue": "31.93",
                                                     "convertedFromCurrency": "EUR"},
                             "totalFeeAmount": {"value": "5.18", "currency": "USD"},
                             "orderLineItems": [{"lineItemId": "9A"}], "references": [{"referenceId": "X1", "referenceType": "ITEM_ID"}]}),
            # 纠纷退货：旧数据只有结算净额 114.23 USD，订单 100 GBP：比例 1.14 在 GBP 全额区间里 → 估算全额
            ec.parse_refund({"transactionId": "RRP-B", "orderId": "B-1", "buyer": {"username": "b_b-1"}, "transactionStatus": "PAYOUT",
                             "transactionDate": "2026-11-12T05:59:31.425Z", "amount": {"value": "114.23", "currency": "USD"}}),
            # 取消交易：部分退款
            ec.parse_refund({"transactionId": "RRP-C", "orderId": "C-1", "buyer": {"username": "b_c-1"},
                             "transactionStatus": "FUNDS_AVAILABLE_FOR_PAYOUT", "transactionDate": "2026-11-12T01:00:00.000Z",
                             "amount": {"value": "18.40", "currency": "USD"},
                             "totalFeeBasisAmount": {"value": "21.60", "currency": "USD", "convertedFromValue": "20",
                                                     "convertedFromCurrency": "EUR"}}),
            # 纠纷（按订单行对上），只有净额：5 ÷ 20 低于全额区间 → 估算部分退款
            ec.parse_refund({"transactionId": "RRP-D", "orderId": "D-1", "buyer": {"username": "b_d-1"}, "transactionStatus": "PAYOUT",
                             "transactionDate": "2026-11-12T02:00:00.000Z", "amount": {"value": "5", "currency": "USD"}}),
        ]
        ec.save(db, "STORE_A", "REFUND", refunds)


def _rows(client, **params):
    r = client.get("/internal/ebay-cases", params={"kind": "REFUND", **params})
    assert r.status_code == 200, r.text
    return {x["external_id"]: x for x in r.json()}


def test_parse_refund_keeps_gross_and_local_amount():
    from app import ebay_cases as ec

    r = ec.parse_refund({"transactionId": "T", "orderId": "O", "transactionMemo": "", "bookingEntry": "DEBIT",
                         "amount": {"value": "9.18", "currency": "USD", "convertedFromValue": "8.5", "convertedFromCurrency": "EUR",
                                    "exchangeRate": "1.08"},
                         "totalFeeBasisAmount": {"value": "10.8", "currency": "USD", "convertedFromValue": "10", "convertedFromCurrency": "EUR"},
                         "totalFeeAmount": {"value": "1.62", "currency": "USD"}})
    assert r["amount"] == 9.18 and r["currency"] == "USD"
    assert r["detail"] == {"booking": "DEBIT", "gross_amount": 10.8, "gross_currency": "USD", "fee_amount": 1.62,
                           "local_amount": 10.0, "local_currency": "EUR", "net_local_amount": 8.5, "net_local_currency": "EUR",
                           "exchange_rate": "1.08"}
    bad = ec.parse_refund({"transactionId": "T", "amount": {"value": "1", "currency": "USD", "convertedFromValue": "x",
                                                            "convertedFromCurrency": "EUR"}})
    assert bad["detail"] == {}


def test_refund_info_types_amounts_and_filters(client):
    _seed()
    rows = _rows(client)
    a, b, c, d = (rows[k]["refund"] for k in ("RRP-A", "RRP-B", "RRP-C", "RRP-D"))
    assert (a["type"], a["full"], a["local_amount"], a["local_currency"], a["memo"]) == ("manual", "full", 31.93, "EUR", "Refund")
    assert a["type_label"] == "主动退款" and a["linked"] == [] and a["skus"] == ["MCD-1"]
    assert (b["type"], b["full"], b["local_amount"], b["local_from"], b["estimated"]) == ("aftersales", "full", 100.0, "estimate", True)
    assert b["reason"] == "ORDERED_WRONG_ITEM" and b["reason_label"] and b["linked"][0]["external_id"] == "5333110234"
    assert b["linked"][0]["action_url"].endswith("5333110234")
    assert (c["type"], c["full"], c["local_amount"], c["reason"]) == ("cancel", "partial", 20.0, "ORDER_MISTAKE")
    assert (d["type"], d["full"], d["estimated"], d["local_amount"], d["linked"][0]["kind"]) == ("aftersales", "partial", True, None, "CASE")
    assert a["estimated"] is False and c["estimated"] is False

    # 筛选：类型、全额 / 部分、原因、纠纷编号、SKU
    assert set(_rows(client, refund_type="aftersales")) == {"RRP-B", "RRP-D"}
    assert set(_rows(client, refund_type="manual")) == {"RRP-A"}
    assert set(_rows(client, refund_full="partial")) == {"RRP-C", "RRP-D"}
    assert set(_rows(client, refund_reason="ORDER_MISTAKE")) == {"RRP-C"}
    assert set(_rows(client, q="5333110234", q_field="linked_id", match="exact")) == {"RRP-B"}
    assert set(_rows(client, q="das,bmw", q_field="sku", match="prefix")) == {"RRP-C", "RRP-D"}
    assert set(_rows(client, refund_type="aftersales", limit=1)) <= {"RRP-B", "RRP-D"} and len(_rows(client, refund_type="aftersales", limit=1)) == 1
    assert client.get("/internal/ebay-cases", params={"kind": "REFUND", "refund_type": "x"}).status_code == 422

    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "REFUND"}).json()
    assert vc["refund_types"] == {"manual": 1, "aftersales": 2, "cancel": 1}
    assert vc["refund_full"] == {"full": 2, "partial": 2}
    opts = client.get("/internal/ebay-cases/options", params={"kind": "REFUND"}).json()
    assert opts["reasons"] == ["ITEM_NOT_RECEIVED", "ORDERED_WRONG_ITEM", "ORDER_MISTAKE"]
    # 页签按本系统的处理状态分：一开始都是待处理；标了处理中 / 已处理才移过去
    assert vc["views"] == {"all": 4, "pending": 4, "processing": 0, "done": 0}
    ids = {k: v["id"] for k, v in rows.items()}
    client.post("/internal/ebay-cases/batch", json={"ids": [ids["RRP-A"]], "action": "state", "state": "DONE"})
    client.post("/internal/ebay-cases/batch", json={"ids": [ids["RRP-B"]], "action": "state", "state": "PROCESSING"})
    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "REFUND"}).json()
    assert vc["views"] == {"all": 4, "pending": 2, "processing": 1, "done": 1}
    assert set(_rows(client, view="done")) == {"RRP-A"} and _rows(client, view="done")["RRP-A"]["view"] == "done"
    assert set(_rows(client, view="pending")) == {"RRP-C", "RRP-D"}
    # 其他类型不带 refund
    assert "refund" not in client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()[0]
