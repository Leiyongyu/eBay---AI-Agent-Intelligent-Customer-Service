"""售后管理商品信息入库：订单商品明细随订单同步存库、按“商品号-交易号 / 订单号 / 买家+商品号”对应售后单、
刊登信息定时补拉（优先级、每轮上限、失败重试）、列表接口直接带上商品信息；数据源缓存同一个 key 并发只加载一次。"""
import threading
import time
from datetime import timedelta


def order(oid, buyer, lines, legacy=None, created="2026-11-01T10:00:00.000Z"):
    return {"orderId": oid, "legacyOrderId": legacy or oid, "creationDate": created, "lastModifiedDate": created,
            "orderFulfillmentStatus": "FULFILLED", "orderPaymentStatus": "PAID", "buyer": {"username": buyer},
            "pricingSummary": {"total": {"value": "10", "currency": "EUR"}}, "paymentSummary": {}, "cancelStatus": {},
            "lineItems": [{"legacyItemId": item, "lineItemId": line, "sku": sku, "title": f"T {item}", "quantity": qty,
                           "lineItemCost": {"value": price, "currency": "EUR"}} for item, line, sku, qty, price in lines]}


def seed_orders():
    from app import customers
    from app.db import session_scope

    with session_scope() as db:
        customers.save_orders(db, "STORE_A", [customers.parse_order(o) for o in (
            order("01-1", "Buyer_X", [("110000000001", "9001", "SKU-A-0557", 1, "49.90")]),
            order("01-2", "buyer_y", [("110000000002", "9002", "SKU-B", 2, "20.00"), ("110000000003", "9003", "SKU-C", 1, "5.00")]),
            order("01-3", "buyer_z", [("110000000004", "9004", "SKU-D", 1, "7.00")], created="2026-10-01T10:00:00.000Z"),
        )])


def add_case(db, kind, ext, order_id=None, item_id=None, buyer=None, is_open=True, title=None):
    from app.models import EbayCase

    c = EbayCase(store_id="STORE_A", kind=kind, external_id=ext, order_id=order_id, item_id=item_id, buyer=buyer,
                 is_open=is_open, title=title)
    db.add(c)
    db.flush()
    return c


def test_order_lines_saved_and_updated(client):
    from sqlalchemy import select

    from app import customers
    from app.db import session_scope
    from app.models import EbayOrderLine

    seed_orders()
    with session_scope() as db:
        lines = {ln.line_key: ln for ln in db.scalars(select(EbayOrderLine))}
        assert set(lines) == {"110000000001-9001", "110000000002-9002", "110000000003-9003", "110000000004-9004"}
        a = lines["110000000001-9001"]
        assert (a.order_id, a.buyer, a.sku, a.quantity, a.price_value) == ("01-1", "buyer_x", "SKU-A-0557", 1, "49.90")
        # 再同步一次（价格变了）：更新，不重复
        customers.save_orders(db, "STORE_A", [customers.parse_order(order("01-1", "buyer_x", [("110000000001", "9001", "SKU-A-0557", 1, "45.00")]))])
        rows = list(db.scalars(select(EbayOrderLine).where(EbayOrderLine.order_id == "01-1")))
        assert len(rows) == 1 and rows[0].price_value == "45.00"
        assert not customers.needs_lines_backfill(db, "STORE_A")


def test_needs_backfill_when_orders_have_no_lines(client):
    from app import customers
    from app.db import session_scope
    from app.models import EbayOrder

    with session_scope() as db:
        assert not customers.needs_lines_backfill(db, "STORE_A")  # 还没有订单：第一次同步本来就拉整个窗口
        db.add(EbayOrder(store_id="STORE_A", order_id="old-1"))
        db.flush()
        assert customers.needs_lines_backfill(db, "STORE_A")


def test_extras_match_and_list_includes_them(client):
    from app.case_extras import extras_for
    from app.db import session_scope
    from app.models import EbayListingInfo

    seed_orders()
    with session_scope() as db:
        inq = add_case(db, "INQUIRY", "5001", order_id="110000000001-9001", item_id="110000000001", buyer="Buyer_X")
        ret = add_case(db, "RETURN", "5002", order_id="01-2", item_id="110000000003", buyer="buyer_y")
        fb = add_case(db, "FEEDBACK", "5003", order_id="110000000004-1234", item_id="110000000004", buyer="BUYER_Z")  # 交易号对不上
        none = add_case(db, "CASE", "5004", order_id="1-2", item_id="119999999999", buyer="nobody", title="Old title")
        db.add(EbayListingInfo(store_id="STORE_A", item_id="110000000001", picture_url="https://img/1.jpg", sku="LIST-SKU", found=True))
        db.add(EbayListingInfo(store_id="STORE_A", item_id="119999999999", picture_url="https://img/9.jpg", sku="SKU-9-0557", found=True))
        db.flush()
        ex = extras_for(db, [inq, ret, fb, none])
        assert ex[inq.id]["sku"] == "SKU-A-0557" and ex[inq.id]["picture_url"] == "https://img/1.jpg"
        assert ex[inq.id]["price"] == {"value": "49.90", "currency": "EUR"} and ex[inq.id]["order_id"] == "01-1"
        assert ex[ret.id]["sku"] == "SKU-C" and ex[ret.id]["quantity"] == 1  # 多商品订单按商品号取对应那行
        assert ex[fb.id]["order_found"] and ex[fb.id]["sku"] == "SKU-D"  # 按 买家 + 商品号 找到
        assert not ex[none.id]["order_found"] and ex[none.id]["sku"] == "SKU-9-0557"  # 订单没有：用刊登信息
        assert ex[none.id]["title"] == "Old title" and ex[none.id]["stock_sku"]
        inq_id = inq.id

    rows = client.get("/internal/ebay-cases", params={"kind": "INQUIRY"}).json()
    row = next(r for r in rows if r["id"] == inq_id)
    assert row["extras"]["sku"] == "SKU-A-0557" and row["extras"]["picture_url"] == "https://img/1.jpg"
    assert client.get(f"/internal/ebay-cases/{inq_id}/extras").json()["sku"] == "SKU-A-0557"


def test_refresh_listings_priority_budget_and_retry(client, monkeypatch):
    from sqlalchemy import select

    from app import case_extras
    from app.db import session_scope, utcnow
    from app.models import EbayListingInfo

    calls = []

    class FakeSource:
        def listing_brief(self, store_id, item_id):
            calls.append(item_id)
            if item_id == "3":
                raise RuntimeError("timeout")
            if item_id == "4":
                return None  # 不是本店的 / 已删除
            return {"item_id": item_id, "title": f"T{item_id}", "sku": f"S{item_id}", "picture_url": f"https://img/{item_id}"}

    monkeypatch.setattr("app.sources.get_ebay_source", lambda: FakeSource())
    with session_scope() as db:
        add_case(db, "FEEDBACK", "f1", item_id="1", is_open=False)
        add_case(db, "INQUIRY", "i1", item_id="2", is_open=True)
        add_case(db, "RETURN", "r1", item_id="3", is_open=True)
        add_case(db, "CASE", "c1", item_id="4", is_open=False)
        add_case(db, "INQUIRY", "i2", item_id="2", is_open=False)  # 同一商品只拉一次

    r = case_extras.refresh_listings("STORE_A", budget=3)
    assert r == {"fetched": 2, "failed": 1, "pending": 1}
    assert calls[:3] and set(calls) == {"2", "3", "4"}  # 待处理 / 非评价优先，评价排最后
    with session_scope() as db:
        info = {i.item_id: i for i in db.scalars(select(EbayListingInfo))}
    assert info["2"].picture_url == "https://img/2" and info["4"].found is False and info["3"].error

    calls.clear()
    r = case_extras.refresh_listings("STORE_A")  # 下一轮：只拉剩下的；失败的还没到重试时间
    assert calls == ["1"] and r["pending"] == 0
    calls.clear()
    assert case_extras.refresh_listings("STORE_A") == {"fetched": 0, "failed": 0, "pending": 0} and not calls

    with session_scope() as db:  # 失败超过重试间隔、成功的超过过期时间：重新拉
        for i in db.scalars(select(EbayListingInfo).where(EbayListingInfo.item_id.in_(["2", "3"]))):
            i.fetched_at = utcnow() - case_extras.LISTING_STALE - timedelta(minutes=1)
    case_extras.refresh_listings("STORE_A")
    assert sorted(calls) == ["2", "3"]


def test_source_cache_loads_once_under_concurrency():
    from app.sources.ebay import EbaySource

    src = EbaySource(api_factory=lambda sid: None)
    loads = []

    def loader():
        loads.append(1)
        time.sleep(0.2)
        return ["orders"]

    out = []
    threads = [threading.Thread(target=lambda: out.append(src._cached(("S", "orders"), loader))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loads) == 1 and out == [["orders"]] * 6
    assert src._cached(("S", "other"), lambda: 2) == 2  # 别的 key 不受影响
