"""议价：查有感兴趣买家的商品（站点头、分页、空结果）、发报价（一次一个商品、折扣 / 价格二选一、只有真实收发的店铺能发）、
记录与最近 7 天已发提示。不连真实 eBay（假的 HTTP 传输）。"""
import json

import httpx
import pytest


class Calls(list):
    """发给假 eBay 的请求；rule = eBay 现在接受的有效期，stock = 各刊登的可售数量（没写的按 3 件）。"""

    def __init__(self):
        super().__init__()
        self.rule = {"duration": {"unit": "DAY", "value": 4}}
        self.stock: dict = {}


@pytest.fixture()
def fake_ebay(monkeypatch):
    from app import offers
    from app.config import get_settings

    calls = Calls()
    rule, stock = calls.rule, calls.stock

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("find_eligible_items"):
            if req.headers["X-EBAY-C-MARKETPLACE-ID"] != "EBAY_DE":
                return httpx.Response(204)
            return httpx.Response(200, json={"eligibleItems": [{"listingId": "397158807671"}, {"listingId": "397158807672"}],
                                             "total": 2})
        body = json.loads(req.content)
        if body["offerDuration"] != rule["duration"]:  # eBay 只接受规定的有效期
            d = rule["duration"]
            return httpx.Response(400, json={"errors": [{"errorId": 150027, "longMessage":
                                  f"Offer is invalid. Please specify a valid duration of {d['value']} {d['unit']}."}]})
        if body["offeredItems"][0]["listingId"] == "397158807672":
            return httpx.Response(400, json={"errors": [{"errorId": 150022, "longMessage": "No interested buyers"}]})
        return httpx.Response(201, json={"offers": [{"offerId": "O1", "offerStatus": "PENDING", "buyer": {"maskedUsername": "b***r"}}]})

    monkeypatch.setattr(offers, "http_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(offers, "_headers", lambda store_id, mk: {"X-EBAY-C-MARKETPLACE-ID": mk, "Authorization": "Bearer t"})
    offers._cache.clear()
    monkeypatch.setattr(offers, "_duration", {"unit": "DAY", "value": 4})
    monkeypatch.setattr(offers, "available_quantity", lambda store_id, lid: stock.get(lid, 3))
    s = get_settings()
    monkeypatch.setattr(s, "ebay_channel", "ebay")
    monkeypatch.setattr(s, "ebay_live_stores", "STORE_A")
    return calls


def test_eligible_send_and_history(client, fake_ebay):
    r = client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE"}).json()
    assert [i["listing_id"] for i in r["items"]] == ["397158807671", "397158807672"] and r["live_reason"] is None
    # 站点不对：eBay 返回空
    assert client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_US"}).json()["items"] == []
    assert client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_XX"}).status_code == 422

    # 折扣和价格二选一、折扣范围
    base = {"store_id": "STORE_A", "marketplace_id": "EBAY_DE", "listing_ids": ["397158807671"]}
    assert client.post("/internal/offers/send", json=base).status_code == 422
    assert client.post("/internal/offers/send", json={**base, "discount_percentage": "3"}).status_code == 422
    assert client.post("/internal/offers/send", json={**base, "discount_percentage": "10",
                                                      "price": {"value": "9", "currency": "EUR"}}).status_code == 422

    r = client.post("/internal/offers/send", json={**base, "listing_ids": ["397158807671", "397158807672"],
                                                   "discount_percentage": "10", "message": "Special price for you!"}).json()
    assert r["sent"] == 1 and r["failed"] == 1
    ok, bad = r["results"]
    assert ok["offers_count"] == 1 and ok["response"]["offers"][0]["buyer"] == "b***r"
    assert "150022" in bad["error"]
    sent = [json.loads(c.content) for c in fake_ebay if c.method == "POST"]
    assert sent[0] == {"offeredItems": [{"listingId": "397158807671", "quantity": 1, "discountPercentage": "10"}],
                       "offerDuration": {"unit": "DAY", "value": 4}, "allowCounterOffer": False,
                       "message": "Special price for you!"}
    assert all(c.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE" for c in fake_ebay if c.method == "POST")

    assert ok["quantity"] == 1 and ok["duration_days"] == 4
    # 页面传来的数量、有效期一律不用：数量固定 1，有效期按 eBay 规定
    r = client.post("/internal/offers/send", json={**base, "discount_percentage": "5", "quantity": 9, "duration_days": 7}).json()
    assert r["sent"] == 1 and json.loads(fake_ebay[-1].content)["offeredItems"][0]["quantity"] == 1
    assert json.loads(fake_ebay[-1].content)["offerDuration"] == {"unit": "DAY", "value": 4}

    # 按价格发
    r = client.post("/internal/offers/send", json={**base, "price": {"value": "19.9", "currency": "eur"}}).json()
    assert r["sent"] == 1
    assert json.loads(fake_ebay[-1].content)["offeredItems"][0]["price"] == {"value": "19.90", "currency": "EUR"}

    # 列表上标出最近 7 天发过的；记录能查到
    items = client.get("/internal/offers/eligible", params={"store_id": "STORE_A", "marketplace_id": "EBAY_DE"}).json()["items"]
    assert items[0]["last_offer"]["status"] == "SENT" and items[1]["last_offer"] is None
    hist = client.get("/internal/offers/history").json()
    assert len(hist) == 4 and {h["status"] for h in hist} == {"SENT", "FAILED"}


def test_send_refused_for_non_live_store(client, fake_ebay, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ebay_live_stores", "SOME_OTHER")
    r = client.post("/internal/offers/send", json={"store_id": "STORE_A", "marketplace_id": "EBAY_DE",
                                                   "listing_ids": ["397158807671"], "discount_percentage": "10"})
    assert r.status_code == 422 and "没有开启议价" in r.json()["detail"]
    assert not [c for c in fake_ebay if c.method == "POST"]


def test_offer_only_store_can_send_without_live_messaging(client, fake_ebay, monkeypatch):
    """“可发报价的店铺”只开议价：能发报价，但不算对接 eBay 真实收发。"""
    from app.config import get_settings
    from app.models import Store
    from app.sending.channel import is_live_store

    s = get_settings()
    monkeypatch.setattr(s, "ebay_live_stores", "SOME_OTHER")
    monkeypatch.setattr(s, "offer_live_stores", "store_a")
    r = client.post("/internal/offers/send", json={"store_id": "STORE_A", "marketplace_id": "EBAY_DE",
                                                   "listing_ids": ["397158807671"], "discount_percentage": "10"}).json()
    assert r["sent"] == 1
    assert not is_live_store(Store(id="STORE_A", name="x"))



def test_quantity_is_one_and_sold_out_not_sent(client, fake_ebay):
    fake_ebay.stock.update({"397158807671": 14, "397158807672": 0})
    r = client.post("/internal/offers/send", json={"store_id": "STORE_A", "marketplace_id": "EBAY_DE",
                                                   "listing_ids": ["397158807671", "397158807672"], "discount_percentage": "5"}).json()
    ok, bad = r["results"]
    assert ok["status"] == "SENT" and ok["quantity"] == 1
    assert bad["status"] == "FAILED" and "可售数量" in bad["error"]
    posts = [json.loads(c.content) for c in fake_ebay if c.method == "POST"]
    assert len(posts) == 1 and posts[0]["offeredItems"][0]["quantity"] == 1


def test_duration_follows_ebay_rule_change(client, fake_ebay):
    """eBay 改了有效期规则（150027）：按它要求的天数重发一次，并记住给后面的报价用。"""
    from app import offers

    fake_ebay.rule["duration"] = {"unit": "DAY", "value": 6}
    base = {"store_id": "STORE_A", "marketplace_id": "EBAY_DE", "listing_ids": ["397158807671"], "discount_percentage": "5"}
    r = client.post("/internal/offers/send", json=base).json()
    posts = [json.loads(c.content)["offerDuration"]["value"] for c in fake_ebay if c.method == "POST"]
    assert posts == [4, 6] and r["sent"] == 1 and r["results"][0]["duration_days"] == 6
    assert offers._duration == {"unit": "DAY", "value": 6}
    assert client.get("/internal/offers/stores").json()["duration_days"] == 6
    client.post("/internal/offers/send", json=base)
    assert [json.loads(c.content)["offerDuration"]["value"] for c in fake_ebay if c.method == "POST"] == [4, 6, 6]


def test_required_duration_parsing():
    import httpx as h

    from app.offers import required_duration

    msg = {"errors": [{"errorId": 150027, "message": "Offer is invalid. Please specify a valid duration of 4 DAY."}]}
    assert required_duration(h.Response(400, json=msg)) == {"unit": "DAY", "value": 4}
    par = {"errors": [{"errorId": 150027, "parameters": [{"name": "durationValue", "value": "3"}, {"name": "durationUnit", "value": "DAY"}]}]}
    assert required_duration(h.Response(400, json=par)) == {"unit": "DAY", "value": 3}
    assert required_duration(h.Response(400, json={"errors": [{"errorId": 150015}]})) is None
    assert required_duration(h.Response(400, text="oops")) is None
