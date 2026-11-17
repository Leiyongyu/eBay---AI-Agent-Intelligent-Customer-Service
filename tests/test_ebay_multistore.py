"""多店铺：每个店铺用 store_credentials 里自己的秘钥同步与查询，数据按店铺隔离。全部用假 eBay，不发真实请求。"""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app import config
from app.config import get_settings
from app.security import crypto

TOKEN_A = "v^1.1#i^1#FAKE-STORE-A-" + "a" * 72
TOKEN_B = "v^1.1#i^1#FAKE-STORE-B-" + "b" * 72
TOKEN_BAD = "v^1.1#i^1#FAKE-STORE-X-" + "x" * 72
ACCESS = {TOKEN_A: "AT-A", TOKEN_B: "AT-B"}
SELLERS = {TOKEN_A: "seller-a", TOKEN_B: "seller-b", TOKEN_BAD: "seller-x"}


# 样例时间放在前天，始终在拉取回溯期内（不要写死日期）
AT = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT08:00:00.000Z")


def _msg(mid, sender, recipient, body, at=AT):
    return {"messageId": mid, "senderUsername": sender, "recipientUsername": recipient, "messageBody": body,
            "createdDate": at}


CONVS = {  # 每个访问令牌只看得到自己店铺的会话
    "AT-A": [{"conversationId": "CA1", "conversationType": "FROM_MEMBERS", "createdDate": AT,
              "latestMessage": _msg("MA1", "buyer_1", "seller-a", "Hallo, passt das?")}],
    "AT-B": [{"conversationId": "CB1", "conversationType": "FROM_MEMBERS", "createdDate": AT,
              "latestMessage": _msg("MB1", "buyer_2", "seller-b", "Where is my order?")},
             {"conversationId": "CB2", "conversationType": "FROM_MEMBERS", "createdDate": AT,
              "latestMessage": _msg("MB2", "buyer_3", "seller-b", "Is it in stock?")}],
}
MESSAGES = {"CA1": [_msg("MA1", "buyer_1", "seller-a", "Hallo, passt das?")],
            "CB1": [_msg("MB1", "buyer_2", "seller-b", "Where is my order?")],
            "CB2": [_msg("MB2", "buyer_3", "seller-b", "Is it in stock?")]}


def fake_ebay(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/identity/v1/oauth2/token":
        form = parse_qs(request.content.decode())
        if form["grant_type"] == ["client_credentials"]:
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 7200})
        refresh = form["refresh_token"][0]
        if refresh not in ACCESS:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
        return httpx.Response(200, json={"access_token": ACCESS[refresh], "expires_in": 7200})
    assert request.method == "GET", "只读：不应发出 GET 以外的请求"
    token = request.headers["Authorization"].split(" ", 1)[1]
    if path == "/commerce/message/v1/conversation":
        items = CONVS[token] if request.url.params["conversation_type"] == "FROM_MEMBERS" else []
        return httpx.Response(200, json={"conversations": items, "total": len(items)})
    if path.startswith("/commerce/message/v1/conversation/"):
        cid = path.rsplit("/", 1)[1]
        assert cid.startswith("C" + token[-1]), "店铺令牌不应读到别家会话"
        return httpx.Response(200, json={"messages": MESSAGES[cid], "total": len(MESSAGES[cid])})
    return httpx.Response(404)


@pytest.fixture()
def stores(app_env, monkeypatch, tmp_path):
    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-id")
    monkeypatch.delenv("EBAY_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")  # 样例消息是固定日期，不受保留期影响（保留期见 test_retention.py）
    config.get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "credentials_master_key", crypto.new_master_key())
    from app.db import session_scope
    from app.ebay.auth import reset_store_managers
    from app.ebay.profile import SellerProfile
    from app.security.store_credentials import import_credentials

    wb = Workbook()
    ws = wb.active
    ws.append(["序号", "店铺名称", "平台", "店铺账号", "店铺IP", "IP已过期", "browserId", "站点ID", "秘钥"])
    ws.append([1, "ZN-Shop-A", "eBay-德国", "a", "", "否", "B1", "23", TOKEN_A])
    ws.append([2, "ZN-Shop-B", "eBay-美国", "b", "", "否", "B2", "1", TOKEN_B])
    ws.append([3, "ZN-Shop-X", "eBay-英国", "x", "", "否", "B3", "4", TOKEN_BAD])
    wb.save(tmp_path / "s.xlsx")
    with session_scope() as db:
        import_credentials(db, tmp_path / "s.xlsx", "test",
                           profile_fetcher=lambda secret: SellerProfile(user_id=SELLERS[secret],
                                                                        store_name=SELLERS[secret].upper()))
    reset_store_managers()
    yield
    reset_store_managers()
    config.get_settings.cache_clear()


def store_ids():
    from app.db import session_scope
    from app.models import Store

    with session_scope() as db:
        return {s.ebay_user_id: s.id for s in db.scalars(select(Store).where(Store.ebay_user_id.is_not(None)))}


def test_each_store_syncs_with_its_own_token(stores):
    from app.db import session_scope
    from app.ebay.auth import MESSAGE_SCOPE, for_store
    from app.ebay.browse_api import AppTokenManager, BrowseClient
    from app.ebay.message_api import MessageApiClient
    from app.ebay.sync import EbayMessageSync
    from app.models import Conversation, EbaySyncState

    http = httpx.Client(transport=httpx.MockTransport(fake_ebay))
    sync = EbayMessageSync(
        browse=BrowseClient(AppTokenManager(http), http, sleep=lambda s: None),
        client_factory=lambda t: MessageApiClient(for_store(t.store_id, MESSAGE_SCOPE, client=http), http,
                                                  sleep=lambda s: None))
    result = sync.run()
    ids = store_ids()
    a, b, x = ids["seller-a"], ids["seller-b"], ids["seller-x"]
    assert result["stores"][a]["ok"] and result["stores"][b]["ok"]
    assert not result["stores"][x]["ok"] and "invalid_grant" in result["stores"][x]["types"]["FROM_MEMBERS"]["error"]
    assert TOKEN_BAD not in str(result)  # 错误信息里不含秘钥
    with session_scope() as db:
        by_store = {}
        for c in db.scalars(select(Conversation).where(Conversation.channel_source == "ebay")):
            by_store.setdefault(c.store_id, set()).add(c.ebay_conversation_id)
        assert by_store == {a: {"CA1"}, b: {"CB1", "CB2"}}
        states = {(st.store_id, st.conversation_type): st for st in db.scalars(select(EbaySyncState))}
        assert states[(a, "FROM_MEMBERS")].last_success_at and states[(b, "FROM_MEMBERS")].last_success_at
        assert states[(x, "FROM_MEMBERS")].last_error  # 失效店铺单独记错，不影响其他店铺


# ---------------------------------------------------------------- 只读工具数据源
class FakeTokens:
    def __init__(self, token):
        self.token = token

    def access_token(self):
        return self.token

    def invalidate(self):
        pass


GETITEM = """<?xml version="1.0"?><GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><Item>
<ItemID>{item}</ItemID><Site>Germany</Site><Title>Turbolader BMW</Title><SKU>BMW-1</SKU><Quantity>10</Quantity>
<Seller><UserID>{seller}</UserID></Seller><DispatchTimeMax>1</DispatchTimeMax>
<SellingStatus><QuantitySold>4</QuantitySold><CurrentPrice currencyID="EUR">99.00</CurrentPrice><ListingStatus>Active</ListingStatus></SellingStatus>
<ItemSpecifics><NameValueList><Name>OE/OEM Referenznummer(n)</Name><Value>11657635803</Value></NameValueList></ItemSpecifics>
<ItemCompatibilityList><Compatibility><NameValueList><Name>Make</Name><Value>BMW</Value></NameValueList>
<NameValueList><Name>Model</Name><Value>3er</Value></NameValueList></Compatibility></ItemCompatibilityList>
</Item></GetItemResponse>"""


def fake_api_http(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("X-EBAY-API-IAF-TOKEN") or request.headers.get("Authorization", "").split(" ", 1)[-1]
    if request.url.path == "/ws/api.dll":
        assert request.headers["X-EBAY-API-CALL-NAME"] in ("GetItem", "GetSellerList")
        if request.headers["X-EBAY-API-CALL-NAME"] == "GetItem":
            item = request.content.decode().split("<ItemID>")[1].split("<")[0]
            seller = "seller-a" if item == "111" else "seller-b"  # 111 是 A 店的刊登，222 是 B 店的
            return httpx.Response(200, content=GETITEM.format(item=item, seller=seller).encode())
    assert request.method == "GET"
    if request.url.path == "/sell/fulfillment/v1/order":
        orders = {"AT-A": [{"orderId": "01-00001-00001", "legacyOrderId": "111-9", "orderFulfillmentStatus": "NOT_STARTED",
                            "buyer": {"username": "buyer_1"}, "lineItems": [{"legacyItemId": "111", "sku": "BMW-1"}]},
                           {"orderId": "01-00002-00002", "legacyOrderId": "111-8", "buyer": {"username": "someone"},
                            "lineItems": [{"legacyItemId": "111"}]}]}.get(token, [])
        return httpx.Response(200, json={"orders": orders, "total": len(orders)})
    if request.url.path.startswith("/post-order/v2/return/search"):
        return httpx.Response(200, json={"members": [{"returnId": "R1", "orderId": "01-00001-00001",
                                                       "state": "ITEM_READY_TO_SHIP", "status": "READY_FOR_SHIPPING"}]})
    if request.url.path.startswith("/post-order/v2/inquiry/search"):
        return httpx.Response(200, json={"members": [{"inquiryId": "I1", "itemId": "111", "transactionId": "9",
                                                       "inquiryStatusEnum": "CLOSED"}]})
    if request.url.path.startswith("/post-order/v2/"):
        return httpx.Response(200, json={"members": [], "cancellations": []})
    return httpx.Response(404)


def make_source():
    from app.sources.ebay import EbayReadOnlyApi, EbaySource

    http = httpx.Client(transport=httpx.MockTransport(fake_api_http))
    tokens = {v: k for k, v in store_ids().items()}
    access = {"seller-a": "AT-A", "seller-b": "AT-B"}
    return EbaySource(api_factory=lambda sid: EbayReadOnlyApi(sid, http=http, tokens=FakeTokens(access[tokens[sid]])))


def test_source_isolation_orders_cases_and_whitelist(stores):
    from app.sources import get_source_for_store
    from app.sources.ebay import EbayReadOnlyApi, EbaySource

    ids = store_ids()
    a, b = ids["seller-a"], ids["seller-b"]
    assert isinstance(get_source_for_store(a), EbaySource)
    assert not isinstance(get_source_for_store("STORE_A"), EbaySource)  # 演示店铺仍用演示数据

    src = make_source()
    listing = src.get_listing(a, "111")
    assert listing["item_specifics"]["OE/OEM Referenznummer(n)"] == "11657635803"
    assert listing["compatibility"][0] == {"Make": "BMW", "Model": "3er"} and listing["marketplace_id"] == "EBAY_DE"
    assert listing["variations"][0]["quantity_available"] == 6
    assert src.get_listing(a, "222") is None  # B 店的刊登：A 店会话查不到
    assert src.get_listing(b, "111") is None

    orders = src.find_orders(a, "buyer_1")
    assert [o["order_id"] for o in orders] == ["01-00001-00001"]
    assert src.find_orders(b, "buyer_1") == []  # B 店令牌看不到 A 店订单
    cases = {(c["case_type"], c["status"]) for c in src.find_cases(a, ["01-00001-00001"])}
    assert cases == {("RETURN", "OPEN"), ("INR", "CLOSED")}

    with pytest.raises(ValueError, match="只读白名单"):
        EbayReadOnlyApi(a, tokens=FakeTokens("x")).trading("ReviseItem", "")
    assert not hasattr(EbayReadOnlyApi, "post") and not hasattr(EbayReadOnlyApi, "put")
