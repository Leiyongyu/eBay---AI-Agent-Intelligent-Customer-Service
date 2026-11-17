"""eBay 实时推送：地址校验、验签、去重、识别店铺并立刻拉取、订阅建立（全部用假的 eBay，不发真实请求）。"""
import base64
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import config
from test_ebay_sync import SELLER, FakeEbay, ebay_env, make_sync  # noqa: F401  ebay_env 是夹具

ENDPOINT = "https://hook.example.test/webhooks/ebay/notification"
TOKEN = "t" * 40
KEY = ec.generate_private_key(ec.SECP256R1())
PEM_ONE_LINE = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode().replace("\n", "")


@pytest.fixture()
def push_env(ebay_env, monkeypatch):
    from app.ebay import notifications

    monkeypatch.setenv("EBAY_NOTIFY_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    config.get_settings.cache_clear()
    notifications.set_public_key_fetcher(lambda kid: PEM_ONE_LINE)  # eBay 返回的 PEM 没有换行
    yield notifications
    notifications.set_public_key_fetcher(None)
    notifications.push_worker.sync_factory = None
    notifications.push_worker._take()


def message(nid="N1", **data):
    return {"metadata": {"topic": "NEW_MESSAGE", "schemaVersion": "1.0"},
            "notification": {"notificationId": nid, "eventDate": "2026-10-27T09:00:00.000Z",
                             "data": {"conversationId": "C9", "recipientUserName": SELLER,
                                      "messageBody": "secret text", **data}}}


def signed(payload, key=KEY):
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = base64.b64encode(key.sign(body, ec.ECDSA(hashes.SHA1()))).decode()
    header = base64.b64encode(json.dumps({"alg": "ECDSA", "kid": "k1", "signature": sig,
                                          "digest": "SHA1"}).encode()).decode()
    return body, {"X-EBAY-SIGNATURE": header, "Content-Type": "application/json"}


def test_challenge_response(push_env, client):
    r = client.get("/webhooks/ebay/notification", params={"challenge_code": "abc"})
    assert r.json() == {"challengeResponse": hashlib.sha256(("abc" + TOKEN + ENDPOINT).encode()).hexdigest()}


def test_signature_dedupe_and_store_sync(push_env, client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EbayNotification, Message

    fake = FakeEbay()
    assert make_sync(fake).run()["ok"]  # 先同步一次：店铺账号 SELLER 已知
    body, headers = signed(message())
    assert client.post("/webhooks/ebay/notification", content=body, headers=headers).status_code == 204
    assert client.post("/webhooks/ebay/notification", content=body, headers=headers).status_code == 204  # 重发去重

    other = ec.generate_private_key(ec.SECP256R1())
    bad_body, bad_headers = signed(message("N2"), key=other)
    assert client.post("/webhooks/ebay/notification", content=bad_body, headers=bad_headers).status_code == 412
    assert client.post("/webhooks/ebay/notification", content=body).status_code == 412  # 没有签名头

    with session_scope() as db:
        rows = db.scalars(select(EbayNotification)).all()
        assert [(r.notification_id, r.store_id, r.ebay_conversation_id, r.status) for r in rows] == [
            ("N1", "REAL_US", "C9", "QUEUED")]
        assert rows[0].payload["notification"]["data"]["messageBody"] == "<已省略>"  # 正文不入库
        db.execute(Message.__table__.delete())

    push_env.push_worker.sync_factory = lambda: make_sync(fake)
    result = push_env.push_worker.run_once()
    assert result["ok"] and list(result["stores"]) == ["REAL_US"]
    assert result["stores"]["REAL_US"]["types"]["FROM_MEMBERS"]["messages_new"] > 0  # 推送触发立刻拉取
    with session_scope() as db:
        assert db.scalar(select(EbayNotification.status)) == "PROCESSED"
    assert push_env.push_worker.run_once() is None  # 没有待处理的


def test_unknown_store_triggers_all(push_env):
    push_env.receive(message("N3", recipientUserName="someone_else", conversationId="UNKNOWN"))
    everything, stores = push_env.push_worker._take()
    assert everything and not stores


class FakeNotificationApi:
    def __init__(self):
        self.destinations, self.subscriptions, self.calls = [], [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.replace("/commerce/notification/v1", ""), request.method
        self.calls.append((method, path, request.headers["Authorization"]))
        if path == "/destination" and method == "GET":
            return httpx.Response(200, json={"destinations": self.destinations})
        if path == "/destination" and method == "POST":
            body = json.loads(request.content)
            assert body["deliveryConfig"] == {"endpoint": ENDPOINT, "verificationToken": TOKEN}
            self.destinations.append({"destinationId": "D1", "status": "ENABLED", **body})
            return httpx.Response(201, headers={"Location": "https://api.ebay.com/commerce/notification/v1/destination/D1"})
        if path == "/subscription" and method == "GET":
            return httpx.Response(200, json={"subscriptions": self.subscriptions})
        if path == "/subscription" and method == "POST":
            body = json.loads(request.content)
            assert (body["topicId"], body["destinationId"]) == ("NEW_MESSAGE", "D1")
            self.subscriptions.append({"subscriptionId": "S1", "status": "ENABLED", **body})
            return httpx.Response(201, headers={"Location": "/commerce/notification/v1/subscription/S1"})
        if path == "/subscription/S1/test" and method == "POST":
            return httpx.Response(202)
        return httpx.Response(404, json={"errors": [{"errorId": 1, "message": "not found"}]})


def test_setup_creates_destination_and_subscriptions_once(push_env):
    from app.db import session_scope
    from app.models import EbayPushSubscription

    fake = FakeNotificationApi()
    api = push_env.NotificationApi(httpx.Client(transport=httpx.MockTransport(fake)))
    r = push_env.setup("tester", send_test=True, api=api, app_token="APP", user_token=lambda sid: "USER")
    assert r["ok"] and r["destination"] == "created" and r["subscribed"] == 1
    assert r["stores"]["REAL_US"] == {"name": r["stores"]["REAL_US"]["name"], "subscription_id": "S1",
                                      "status": "ENABLED", "action": "created", "test": "requested"}
    assert ("POST", "/destination", "Bearer APP") in fake.calls  # 目的地用应用令牌
    assert ("POST", "/subscription", "Bearer USER") in fake.calls  # 订阅用店铺自己的令牌

    again = push_env.setup("tester", api=api, app_token="APP", user_token=lambda sid: "USER")
    assert again["destination"] == "exists" and again["stores"]["REAL_US"]["action"] == "exists"
    with session_scope() as db:
        sub = db.get(EbayPushSubscription, "REAL_US")
        assert (sub.subscription_id, sub.destination_id, sub.status) == ("S1", "D1", "ENABLED")


def test_setup_requires_config(ebay_env):
    from app.ebay.notifications import setup

    r = setup("tester")
    assert not r["ok"] and "EBAY_NOTIFY_ENDPOINT" in r["error"]


def test_paused_sync_records_but_does_not_pull(push_env, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EbayNotification

    monkeypatch.setenv("EBAY_SYNC_ENABLED", "false")
    config.get_settings.cache_clear()
    push_env.receive(message("N9"))
    assert push_env.push_worker._take() == (False, set())  # 没有排队拉取
    with session_scope() as db:
        assert db.scalar(select(EbayNotification.status).where(EbayNotification.notification_id == "N9")) == "PAUSED"
