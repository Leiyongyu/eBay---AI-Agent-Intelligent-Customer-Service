"""Best offer：GetBestOffers 解析、去重更新与过期 / 结束标记、列表计数、回应只限对接店铺、回应请求格式与结果、必须二次确认。"""
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx

from app import config

XML = """<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<ItemBestOffersArray><ItemBestOffers><BestOfferArray>
<BestOffer><BestOfferID>BO-1</BestOfferID><ExpirationTime>{exp}</ExpirationTime>
<Buyer><Email>x@y.de</Email><FeedbackScore>42</FeedbackScore><UserID>Buyer_A</UserID></Buyer>
<Price currencyID="EUR">80.0</Price><Status>Pending</Status><Quantity>1</Quantity><BuyerMessage>Last price?</BuyerMessage>
<BestOfferCodeType>BuyerBestOffer</BestOfferCodeType></BestOffer>
</BestOfferArray><Item><BuyItNowPrice currencyID="EUR">100.0</BuyItNowPrice><ItemID>110000000001</ItemID></Item></ItemBestOffers>
</ItemBestOffersArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></GetBestOffersResponse>"""


def seed(store="STORE_A", hours=20):
    from app import best_offers as bo
    from app.db import session_scope, utcnow

    exp = (utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = bo.parse(ET.fromstring(XML.format(exp=exp)))
    with session_scope() as db:
        r = bo.save(db, store, rows)
    return rows, r


def test_parse_save_list_and_gone(client):
    from app import best_offers as bo
    from app.db import session_scope

    rows, r = seed()
    o = rows[0]
    assert (o["best_offer_id"], o["buyer"], o["buyer_feedback"], o["price"], o["bin_price"], o["currency"], o["status"]) == (
        "BO-1", "buyer_a", 42, 80.0, 100.0, "EUR", "Pending")
    assert "email" not in o and r == {"offers": 1, "new": 1, "gone": 0}
    lst = client.get("/internal/best-offers").json()
    assert len(lst) == 1 and lst[0]["pending"] and lst[0]["percent"] == 80 and lst[0]["type_label"] == "买家报价"
    assert not lst[0]["can_respond"] and lst[0]["respond_blocked"]  # 测试环境发送渠道是 mock、也没有对接店铺：不能回应
    assert client.get("/internal/best-offers/counts").json()["pending"] == 1
    # 下次 eBay 不再返回：没过期的记“已结束”
    with session_scope() as db:
        assert bo.save(db, "STORE_A", [])["gone"] == 1
    assert client.get("/internal/best-offers").json() == []
    assert client.get("/internal/best-offers", params={"tab": "done"}).json()[0]["status"] == "Gone"


def test_respond_only_live_store_and_request(client, monkeypatch):
    from app import best_offers as bo
    from app.db import session_scope
    from app.ebay import auth as ebay_auth
    from app.models import BestOffer

    seed()
    oid = client.get("/internal/best-offers").json()[0]["id"]
    assert client.post(f"/internal/best-offers/{oid}/respond", json={"action": "Decline", "confirmed": True}).status_code == 403
    assert client.post(f"/internal/best-offers/{oid}/respond", json={"action": "Decline"}).status_code == 422  # 没确认

    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    config.get_settings.cache_clear()

    class Tok:
        def access_token(self):
            return "tok"

    monkeypatch.setattr(ebay_auth, "for_store", lambda *a, **k: Tok())
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, content=b"<RespondToBestOfferResponse xmlns='urn:ebay:apis:eBLBaseComponents'><Ack>Success</Ack></RespondToBestOfferResponse>")

    with session_scope() as db:
        o = db.get(BestOffer, oid)
        assert client.post(f"/internal/best-offers/{oid}/respond", json={"action": "Counter", "confirmed": True}).status_code == 422  # 还价没价格
        bo.respond(db, o, "Counter", "tester", price=90, message="Best I can do", http=httpx.Client(transport=httpx.MockTransport(handler)))
        assert (o.status, o.counter_price, o.responded_by) == ("Countered", 90, "tester")
    body = sent[0]
    assert "<Action>Counter</Action>" in body and "<BestOfferID>BO-1</BestOfferID>" in body and 'currencyID="EUR">90.00<' in body
    assert "<SellerResponse>Best I can do</SellerResponse>" in body
    # 已回应过的不能再操作
    assert client.post(f"/internal/best-offers/{oid}/respond", json={"action": "Accept", "confirmed": True}).status_code == 409


def test_respond_ebay_error_recorded(client, monkeypatch):
    from app import best_offers as bo
    from app.db import session_scope
    from app.ebay import auth as ebay_auth
    from app.models import BestOffer

    seed()
    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    config.get_settings.cache_clear()
    monkeypatch.setattr(ebay_auth, "for_store", lambda *a, **k: type("T", (), {"access_token": lambda self: "t"})())
    fail = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<R xmlns='urn:ebay:apis:eBLBaseComponents'><Ack>Failure</Ack>"
                                                                      b"<Errors><LongMessage>Offer expired.</LongMessage></Errors></R>"))
    with session_scope() as db:
        o = db.scalar(__import__("sqlalchemy").select(BestOffer))
        try:
            bo.respond(db, o, "Accept", "tester", http=httpx.Client(transport=fail))
        except bo.OfferError as e:
            assert e.status_code == 422 and "Offer expired" in e.message
        assert o.status == "Pending" and o.response_error == "Offer expired."
