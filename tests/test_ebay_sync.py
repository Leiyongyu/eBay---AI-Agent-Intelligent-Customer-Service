"""eBay 只读消息拉取：用 httpx.MockTransport 模拟 eBay，不发真实请求。"""
import base64
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app import config

SELLER = "stardrive_real"
# eBay 系统通知的正文是整封 HTML 邮件
SYSTEM_HTML = ('<!DOCTYPE html><html><head><style>.x{color:red}</style></head><body>'
               '<div>A buyer opened a return.</div><table><tr><td>Return ID:</td><td>5332019227</td></tr></table>'
               '<script>alert(1)</script></body></html>')


# 样例消息都放在“前天”（UTC）：始终在拉取回溯期（EBAY_SYNC_LOOKBACK_DAYS=7）之内，又始终早于 24 小时
# （自动回复只处理 24 小时内的来信，见 test_auto_reply）；不要写死日期
DAY = (datetime.now(timezone.utc) - timedelta(days=2)).date()


def _ts(hhmm: str) -> str:
    return f"{DAY.isoformat()}T{hhmm}:00.000Z"


def _msg(mid, sender, recipient, body, at):
    return {"messageId": mid, "senderUsername": sender, "recipientUsername": recipient,
            "messageBody": body, "createdDate": at, "subject": "Re: item", "readStatus": False}


CONVS = {
    "FROM_MEMBERS": [
        {"conversationId": "C1", "conversationType": "FROM_MEMBERS", "conversationStatus": "ACTIVE",
         "conversationTitle": "Brake pads", "referenceType": "LISTING", "referenceId": "300000000001",
         "createdDate": _ts("08:00"),
         "latestMessage": _msg("M2", SELLER, "buyer_a", "Thanks, shipped today.", _ts("09:00"))},
        {"conversationId": "C2", "conversationType": "FROM_MEMBERS", "conversationStatus": "ACTIVE",
         "conversationTitle": "Headlight", "createdDate": _ts("10:00"),
         "referenceType": "LISTING", "referenceId": "300000000002",
         "latestMessage": _msg("M3", "buyer_b", SELLER, "Wo ist meine Bestellung?", _ts("10:00"))},
        {"conversationId": "C3", "conversationType": "FROM_MEMBERS", "conversationStatus": "ACTIVE",
         "conversationTitle": "Mirror", "createdDate": _ts("10:30"),
         "referenceType": "LISTING", "referenceId": "300000000003",
         "latestMessage": _msg("M4", "buyer_c", SELLER, "Bonjour", _ts("10:30"))},
        {"conversationId": "C4", "conversationType": "FROM_MEMBERS", "conversationStatus": "ACTIVE",
         "conversationTitle": "Old pump", "createdDate": _ts("10:40"),
         "referenceType": "LISTING", "referenceId": "300000000004",
         "latestMessage": _msg("M5", "buyer_d", SELLER, "Is it still available?", _ts("10:40"))},
    ],
    "FROM_EBAY": [
        {"conversationId": "E1", "conversationType": "FROM_EBAY", "conversationStatus": "ACTIVE",
         "conversationTitle": "Return opened", "createdDate": _ts("11:00"),
         "latestMessage": _msg("S1", "eBay", SELLER, SYSTEM_HTML, _ts("11:00"))},
    ],
}
MESSAGES = {
    "C1": [_msg("M1", "buyer_a", SELLER, "When will it ship?", _ts("08:00")),
           _msg("M2", SELLER, "buyer_a", "Thanks, shipped today.", _ts("09:00"))],
    "C2": [_msg("M3", "buyer_b", SELLER, "Wo ist meine Bestellung?", _ts("10:00"))],
    "C3": [_msg("M4", "buyer_c", SELLER, "Bonjour", _ts("10:30"))],
    "C4": [_msg("M5", "buyer_d", SELLER, "Is it still available?", _ts("10:40"))],
    "E1": [_msg("S1", "eBay", SELLER, SYSTEM_HTML, _ts("11:00"))],
}


# 刊登号 → eBay 返回的刊登站点；不在表里的视为已下架（404）
LISTING_SITES = {
    "300000000001": "EBAY_MOTORS_US",  # 美国汽配频道 → 归为 EBAY_US
    "300000000002": "EBAY_DE",
    "300000000003": "EBAY_FR",  # 店铺没开通法国站
}


class FakeEbay:
    def __init__(self, token_error=None, expire_first_token=False):
        self.requests: list[httpx.Request] = []
        self.browse_lookups: list[str] = []
        self.token_error = token_error
        self.expire_first_token = expire_first_token
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/identity/v1/oauth2/token":
            form = parse_qs(request.content.decode())
            assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"app-id:cert-id").decode()
            if form["grant_type"] == ["client_credentials"]:
                assert form["scope"] == ["https://api.ebay.com/oauth/api_scope"]
                return httpx.Response(200, json={"access_token": "APP", "expires_in": 7200})
            assert form["grant_type"] == ["refresh_token"]
            assert form["scope"] == ["https://api.ebay.com/oauth/api_scope/commerce.message"]
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error, "error_description": "bad"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"AT{self.tokens_issued}", "expires_in": 7200})
        assert request.method == "GET", "只读客户端不应发出 GET 以外的请求"
        if path == "/buy/browse/v1/item/get_item_by_legacy_id":
            assert request.headers["Authorization"] == "Bearer APP"
            self.browse_lookups.append(request.url.params["legacy_item_id"])
            site = LISTING_SITES.get(request.url.params["legacy_item_id"])
            if site is None:
                return httpx.Response(404, json={"errors": [{"errorId": 11001, "message": "not found"}]})
            return httpx.Response(200, json={"listingMarketplaceId": site, "title": "t",
                                             "itemWebUrl": "https://www.ebay.com/itm/x",
                                             "price": {"value": "10.00", "currency": "EUR"}})
        if self.expire_first_token and request.headers["Authorization"] == "Bearer AT1":
            return httpx.Response(401, json={"errors": [{"errorId": 1001, "message": "Invalid access token"}]})
        q = dict(request.url.params)
        if path == "/commerce/message/v1/conversation":
            items = CONVS[q["conversation_type"]]
            off, lim = int(q["offset"]), int(q["limit"])
            page = items[off: off + 1]  # 每页 1 条，测试分页
            more = off + 1 < len(items)
            return httpx.Response(200, json={"conversations": page, "total": len(items), "offset": off, "limit": lim,
                                             **({"next": "next"} if more else {})})
        if path.startswith("/commerce/message/v1/conversation/"):
            cid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"conversationTitle": cid, "conversationStatus": "ACTIVE",
                                             "messages": MESSAGES[cid], "total": len(MESSAGES[cid])})
        return httpx.Response(404)


@pytest.fixture()
def ebay_env(app_env, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-id")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "v^1.1#i^1#r^1#fake-refresh")
    monkeypatch.setenv("EBAY_STORE_ID", "REAL_US")
    monkeypatch.setenv("DATA_RETENTION_DAYS", "0")  # 不受保留期影响（保留期见 test_retention.py）
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def make_sync(fake: FakeEbay):
    from app.ebay.auth import EbayTokenManager
    from app.ebay.browse_api import AppTokenManager, BrowseClient
    from app.ebay.message_api import MessageApiClient
    from app.ebay.sync import EbayMessageSync

    http = httpx.Client(transport=httpx.MockTransport(fake))
    return EbayMessageSync(MessageApiClient(EbayTokenManager(http), http, sleep=lambda s: None),
                           BrowseClient(AppTokenManager(http), http, sleep=lambda s: None))


def test_token_kind():
    from app.ebay.auth import token_kind

    assert token_kind("AgAAAA**AQAAAA**aAAAAA**xyz") == "auth_n_auth"
    assert token_kind("v^1.1#i^1#r^1#p^3#abc") == "oauth"
    assert token_kind("") == "missing"


def test_sync_stores_messages_without_jobs_and_is_idempotent(ebay_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Conversation, Message, ReplyJob, Store

    fake = FakeEbay()
    result = make_sync(fake).run()
    assert result["ok"], result
    assert result["types"]["FROM_MEMBERS"]["messages_new"] == 5
    assert result["types"]["FROM_EBAY"]["messages_new"] == 1

    with session_scope() as db:
        assert db.get(Store, "REAL_US").ebay_user_id == SELLER  # 两个会话的共同参与方即店铺
        c1 = db.scalar(select(Conversation).where(Conversation.ebay_conversation_id == "C1"))
        assert (c1.channel_source, c1.buyer_key, c1.item_id) == ("ebay", "buyer_a", "300000000001")
        roles = [m.sender_role for m in db.scalars(select(Message).where(Message.conversation_id == c1.id)
                                                   .order_by(Message.conversation_seq))]
        assert roles == ["BUYER", "SELLER"]
        e1 = db.scalar(select(Conversation).where(Conversation.ebay_conversation_id == "E1"))
        assert e1.conversation_type == "FROM_EBAY"
        s1 = db.scalar(select(Message).where(Message.conversation_id == e1.id))
        assert s1.sender_role == "SYSTEM"
        assert (s1.body, s1.body_html) == ("A buyer opened a return.\nReturn ID: 5332019227", SYSTEM_HTML)
        assert db.scalar(select(Message.body_html).where(Message.conversation_id == c1.id)) is None  # 纯文本消息不动
        assert db.scalars(select(ReplyJob)).all() == []  # 默认不进入 AI 处理

    detail_calls = len([r for r in fake.requests if r.url.path.count("/conversation/") == 1])
    again = make_sync(fake).run()
    assert again["types"]["FROM_MEMBERS"]["messages_new"] == 0
    assert again["types"]["FROM_MEMBERS"]["conversations_unchanged"] == 4
    assert len([r for r in fake.requests if r.url.path.count("/conversation/") == 1]) == detail_calls  # 未再取详情
    assert all(r.method in ("GET", "POST") for r in fake.requests)
    assert all(r.method == "GET" for r in fake.requests if "/commerce/message/" in r.url.path)


def test_marketplace_resolved_from_listing(ebay_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Conversation, EbayListing

    fake = FakeEbay()
    result = make_sync(fake).run()
    m = result["marketplaces"]
    assert m["conversations_updated"] == 2 and m["site_EBAY_US"] == 1 and m["site_EBAY_DE"] == 1
    assert m["not_enabled"] == 1 and m["listings_not_found"] == 1
    with session_scope() as db:
        site = {c.ebay_conversation_id: (c.marketplace_id, c.marketplace_source)
                for c in db.scalars(select(Conversation).where(Conversation.channel_source == "ebay"))}
        assert site["C1"] == ("EBAY_US", "listing")  # EBAY_MOTORS_US 归为美国站
        assert site["C2"] == ("EBAY_DE", "listing")
        assert site["C3"] == ("UNKNOWN", "listing_not_enabled:EBAY_FR")  # 店铺没开通法国站
        assert site["C4"][0] == "UNKNOWN"  # 刊登已下架
        assert site["E1"][0] == "UNKNOWN"  # eBay 系统消息不关联刊登
        motors = db.get(EbayListing, "300000000001")
        assert (motors.listing_marketplace_id, motors.marketplace_id) == ("EBAY_MOTORS_US", "EBAY_US")

    # 再同步一次：已查到的刊登走缓存，下架的 7 天内不重查
    lookups = len(fake.browse_lookups)
    make_sync(fake).run()
    assert len(fake.browse_lookups) == lookups


def test_expired_access_token_is_refreshed_once(ebay_env):
    fake = FakeEbay(expire_first_token=True)
    result = make_sync(fake).run()
    assert result["ok"], result
    assert fake.tokens_issued == 2


def test_invalid_scope_reports_hint_without_token(ebay_env):
    fake = FakeEbay(token_error="invalid_scope")
    result = make_sync(fake).run()
    assert not result["ok"]
    err = result["types"]["FROM_MEMBERS"]["error"]
    assert "commerce.message" in err and "fake-refresh" not in err


def test_auth_n_auth_token_rejected(ebay_env, monkeypatch):
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "AgAAAA**AQAAAA**legacy")
    config.get_settings.cache_clear()
    result = make_sync(FakeEbay()).run()
    assert "Auth'n'Auth" in result["types"]["FROM_MEMBERS"]["error"]


def test_real_ebay_conversation_cannot_be_sent(ebay_env, client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.ingestion.service import ingest_buyer_message
    from app.models import Conversation

    make_sync(FakeEbay()).run()
    with session_scope() as db:
        conv = db.scalar(select(Conversation).where(Conversation.ebay_conversation_id == "C2"))
        job = ingest_buyer_message(db, conv=conv, ebay_message_id="M9", body="hello?", skip_merge_window=True).job
        job_id, version = job.id, conv.row_version
    r = client.post(f"/internal/jobs/{job_id}/manual-reply", json={
        "text": "Hi", "expected_conversation_version": version, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409 and "只读" in r.json()["detail"]["message"]


def test_buyer_images_downloaded_on_sync_and_expire_by_message_time(ebay_env, monkeypatch):
    """买家发的图片拉到就下载（像真实聊天一样马上能看），保留期按买家发图时间算（MEDIA_RETENTION_DAYS）。"""
    import io

    from PIL import Image
    from sqlalchemy import select

    from app.db import session_scope
    from app.media import service as media
    from app.models import Message, MessageAttachment

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "JPEG")
    monkeypatch.setattr(media, "download", lambda url, client=None, settings=None: buf.getvalue())
    m3 = dict(MESSAGES["C2"][0], messageMedia=[{"mediaName": "image", "mediaType": "IMAGE",
                                                "mediaUrl": "https://i.ebayimg.com/images/g/x/s-l1600.jpg"}])
    monkeypatch.setitem(MESSAGES, "C2", [m3])
    result = make_sync(FakeEbay()).run()
    assert result["types"]["FROM_MEMBERS"]["attachments_stored"] == 1
    with session_scope() as db:
        att = db.scalar(select(MessageAttachment))
        msg = db.get(Message, att.message_id)
        assert att.status == "STORED" and att.local_path
        days = config.get_settings().media_retention_days
        assert abs((att.expires_at - (msg.sent_at + timedelta(days=days))).total_seconds()) < 1
