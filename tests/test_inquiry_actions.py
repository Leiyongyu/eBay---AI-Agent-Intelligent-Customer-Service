"""咨询（未收到货）纠纷处理页：四种处理的请求体、只对接店铺、角色权限、退款金额核对、超时不重试、备注历史。
eBay 用假接口，不联网、不真实处理。"""
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import login, make_user

DETAIL = {"inquiryStatusEnum": "OPEN", "creationReason": "ITEM_NOT_RECEIVED", "initiator": "BUYER",
          "claimAmount": {"value": 146.57, "currency": "GBP"}, "inquiryQuantity": 1,
          "inquiryDetails": {"creationDate": {"value": "2026-11-11T03:27:23.000Z"},
                             "expirationDate": {"value": "2026-11-14T03:27:23.000Z"}},
          "inquiryHistoryDetails": {"history": [
              {"actor": "BUYER", "action": "OPEN", "description": "A CPS case was created.", "date": {"value": "2026-11-11T03:27:23.000Z"}},
              {"actor": "SYSTEM", "action": "AUTO_DISPOSE", "description": "Auto Dispose Approve the case", "date": {"value": "2026-11-11T03:27:29.000Z"}}],
              "shipmentTrackingDetails": {"carrier": "Royal Mail", "trackingNumber": "GN429512355GB", "currentStatus": "DELIVERED"}}}


class FakeApi:
    def __init__(self, detail=None):
        self.detail = detail or DETAIL
        self.calls = 0

    def get(self, path, params=None, auth="Bearer", marketplace=None):
        self.calls += 1
        assert path.startswith("/post-order/v2/inquiry/") and auth == "IAF"
        return self.detail


class FakeEbay:
    def __init__(self, status=200, raise_exc=None):
        self.requests: list[httpx.Request] = []
        self.status, self.raise_exc = status, raise_exc

    def __call__(self, request):
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, json={} if self.status < 400 else {"error": [{"message": "Inquiry already closed"}]})

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture()
def inq(app_env, monkeypatch):
    from app import config, inquiry_actions as ia
    from app.db import session_scope
    from app.models import EbayCase, Store

    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "店铺A")
    config.get_settings.cache_clear()
    monkeypatch.setattr("app.ebay.auth.EbayTokenManager.access_token", lambda self: "AT")
    monkeypatch.setattr(ia, "order_refund_state", lambda store, oid: {"payment_status": "PAID", "refunds": []})
    with session_scope() as db:
        db.add(Store(id="IA", name="店铺A", default_marketplace_id="EBAY_GB"))
        db.add(Store(id="IB", name="店铺B", default_marketplace_id="EBAY_DE"))
        db.flush()
        a = EbayCase(store_id="IA", kind="INQUIRY", external_id="5390255657", order_id="405924410373-1", buyer="matiusuk",
                     status="OPEN", is_open=True, amount=146.57, currency="GBP", opened_at=datetime(2026, 11, 11))
        b = EbayCase(store_id="IB", kind="INQUIRY", external_id="5390099631", buyer="muhammad", status="OPEN", is_open=True,
                     amount=22.64, currency="GBP")
        db.add_all([a, b])
        db.flush()
        ids = a.id, b.id
    yield ia, ids
    config.get_settings.cache_clear()


def _case(cid):
    from app.db import session_scope
    from app.models import EbayCase

    with session_scope() as db:
        c = db.get(EbayCase, cid)
        db.expunge(c)
        return c


class U:
    def __init__(self, role):
        self.role, self.display_name = role, f"u_{role}"


def _run(ia, cid, action, payload, role="CS", fake=None, api=None):
    from app.db import session_scope
    from app.models import EbayCase

    fake = fake or FakeEbay()
    with session_scope() as db:
        c = db.get(EbayCase, cid)
        r = ia.perform(db, c, action, payload, U(role), http=httpx.Client(transport=httpx.MockTransport(fake)),
                       api=api or FakeApi())
    return r, fake


def test_parse_detail():
    from app.inquiry_actions import parse_detail

    d = parse_detail(DETAIL)
    assert d["claim_amount"] == {"value": 146.57, "currency": "GBP"} and d["tracking"]["number"] == "GN429512355GB"
    assert [h["actor"] for h in d["history"]] == ["BUYER", "SYSTEM"] and d["expires_at"].startswith("2026-11-14")


def test_shipment_and_message_bodies(inq, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import AuditEvent, EbayCaseNote

    ia, (a, _) = inq
    r, fake = _run(ia, a, "shipment", {"tracking_number": "GN429512355GB", "carrier": "Royal Mail",
                                       "shipped_at": "2026-11-05T17:51:22Z", "comment": "Delivered to front door"})
    req = fake.requests[0]
    assert req.method == "POST" and req.url.path == "/post-order/v2/inquiry/5390255657/provide_shipment_info"
    assert req.headers["Authorization"] == "IAF AT" and req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
    assert fake.body() == {"trackingNumber": "GN429512355GB", "shippingCarrierName": "Royal Mail", "shippedWithTracking": True,
                           "shippingDate": {"value": "2026-11-05T17:51:22.000Z"},
                           "sellerComments": {"content": "Delivered to front door"}}
    assert r["live"]["status"] == "OPEN"
    with pytest.raises(ia.InquiryError) as e:  # 发信走发件箱：要对得上买家会话
        _run(ia, a, "message", {"text": "Hello, the parcel was delivered."})
    assert e.value.status_code == 409 and "对不上买家会话" in e.value.message
    from app import case_actions_common as common
    from app.models import Conversation, OutboundMessage

    with session_scope() as db:
        db.add(Conversation(store_id="IA", ebay_conversation_id="C-IA", buyer_key="matiusuk", channel_source="ebay"))
    sent = []
    monkeypatch.setattr(common, "post", lambda store, path, body, http=None: sent.append((path, body)) or {})
    r, fake = _run(ia, a, "message", {"text": "Hello, the parcel was delivered."})  # 测试里发送延迟为 0：提交即发
    assert not fake.requests and sent == [("/post-order/v2/inquiry/5390255657/send_message",
                                           {"message": {"content": "Hello, the parcel was delivered."}})]
    with session_scope() as db:
        assert db.get(OutboundMessage, r["queued"]["outbound_id"]).state == "SENT"
        notes = [n.text for n in db.scalars(select(EbayCaseNote).order_by(EbayCaseNote.id))]
        assert notes[0].startswith("[上传运单号] 上传运单号 GN429512355GB") and notes[1].startswith("[给买家发信]")
        assert db.scalar(select(AuditEvent.action).where(AuditEvent.action == "INQUIRY_SHIPMENT"))
    assert _case(a).work_state == "PROCESSING"


def test_validation_and_roles(inq):
    ia, (a, _) = inq
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "shipment", {"carrier": "Royal Mail"})
    assert e.value.status_code == 422 and "运单号" in e.value.message
    for action, payload in (("escalate", {"reason": "SHIPPED_ITEM", "comment": "x"}),
                            ("refund", {"amount": 146.57, "currency": "GBP", "confirm": True})):
        with pytest.raises(ia.InquiryError) as e:
            _run(ia, a, action, payload, role="CS")
        assert e.value.status_code == 403
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "escalate", {"reason": "NOPE", "comment": "x"}, role="AFTERSALES")
    assert e.value.status_code == 422
    _, fake = _run(ia, a, "escalate", {"reason": "SHIPPED_ITEM", "comment": "Tracking shows delivered."}, role="AFTERSALES")
    assert fake.requests[0].url.path.endswith("/escalate")
    assert fake.body() == {"escalateInquiryReason": "SHIPPED_ITEM", "comments": {"content": "Tracking shows delivered."}}


def test_refund_checks_amount_live(inq):
    ia, (a, _) = inq
    fake = FakeEbay()
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "refund", {"amount": 100, "currency": "GBP", "confirm": True}, role="ADMIN", fake=fake)
    assert e.value.status_code == 409 and not fake.requests  # 金额对不上：没有发出退款
    with pytest.raises(ia.InquiryError):
        _run(ia, a, "refund", {"amount": 146.57, "currency": "GBP"}, role="ADMIN", fake=fake)  # 没确认
    assert not fake.requests
    r, fake = _run(ia, a, "refund", {"amount": 146.57, "currency": "GBP", "confirm": True, "comment": "Sorry"}, role="ADMIN")
    assert fake.requests[0].url.path.endswith("/issue_refund") and fake.body() == {"comments": {"content": "Sorry"}}
    assert "GBP 146.57" in r["done"] and _case(a).work_state == "DONE"


def test_refund_blocked_by_ebay_state(inq, monkeypatch):
    """eBay 上已退过款 / 订单已全额退款 / 查不到订单状态：都不发退款请求。"""
    import copy

    ia, (a, _) = inq
    pay = {"amount": 146.57, "currency": "GBP", "confirm": True}
    done = copy.deepcopy(DETAIL)
    done["inquiryDetails"]["refundAmounts"] = {"refundAmount": {"value": 146.57, "currency": "GBP"}}
    fake = FakeEbay()
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "refund", pay, role="ADMIN", fake=fake, api=FakeApi(done))
    assert e.value.status_code == 409 and "已经退过款" in e.value.message
    monkeypatch.setattr(ia, "order_refund_state", lambda s, o: {"payment_status": "FULLY_REFUNDED", "refunds": []})
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "refund", pay, role="ADMIN", fake=fake)
    assert e.value.status_code == 409 and "全额退款" in e.value.message

    def boom(s, o):
        raise RuntimeError("timeout")
    monkeypatch.setattr(ia, "order_refund_state", boom)
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "refund", pay, role="ADMIN", fake=fake)
    assert e.value.status_code == 502 and "查不到订单" in e.value.message
    no_claim = copy.deepcopy(DETAIL)
    no_claim.pop("claimAmount")
    monkeypatch.setattr(ia, "order_refund_state", lambda s, o: None)
    with pytest.raises(ia.InquiryError) as e:  # eBay 没给索赔金额：不用本地金额兜底
        _run(ia, a, "refund", pay, role="ADMIN", fake=fake, api=FakeApi(no_claim))
    assert e.value.status_code == 409
    assert not fake.requests


def test_order_refunds_parse():
    from app.inquiry_actions import order_refunds

    o = {"orderPaymentStatus": "PARTIALLY_REFUNDED",
         "paymentSummary": {"refunds": [{"amount": {"value": "10.00", "currency": "GBP"}, "refundDate": "2026-11-01T00:00:00Z",
                                         "refundStatus": "REFUNDED"}]},
         "lineItems": [{"sku": "A1", "refunds": [{"amount": {"value": "5.5", "currency": "GBP"}, "refundStatus": "PENDING"}]}]}
    r = order_refunds(o)
    assert r["payment_status_label"] == "已部分退款"
    assert [(x["amount"]["value"], x["line"]) for x in r["refunds"]] == [(10.0, None), (5.5, "A1")]
    assert order_refunds({})["refunds"] == []


def test_blocked_store_errors_and_no_retry(inq):
    ia, (a, b) = inq
    fake = FakeEbay()
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, b, "message", {"text": "hi"}, fake=fake)  # 店铺B 没对接
    assert e.value.status_code == 403 and "eBay 后台" in e.value.message and not fake.requests
    ship = {"tracking_number": "GN1", "carrier": "Royal Mail"}
    fake = FakeEbay(raise_exc=httpx.ReadTimeout("t"))
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "shipment", ship, fake=fake)
    assert e.value.status_code == 502 and "不确定" in e.value.message and len(fake.requests) == 1  # 不重试
    with pytest.raises(ia.InquiryError) as e:
        _run(ia, a, "shipment", ship, fake=FakeEbay(status=400))
    assert e.value.status_code == 422 and "Inquiry already closed" in e.value.message


def test_endpoints_full_notes_and_permissions(client, inq, monkeypatch):
    from app import inquiry_actions as ia
    from app.api import ebay_cases as api

    _, (a, b) = inq
    monkeypatch.setattr(ia, "live_detail", lambda store, iid, api_=None: ia.parse_detail(DETAIL))
    monkeypatch.setattr(api, "_order_info", lambda store, oid: {"order_id": oid, "name": "Matus", "packages": []})
    f = client.get(f"/internal/ebay-cases/{a}/full").json()
    assert f["inquiry"]["claim_amount"]["value"] == 146.57 and f["actions"]["blocked"] is None
    assert all(f["actions"]["can"].values())  # 管理员四种都能做
    assert client.get(f"/internal/ebay-cases/{b}/full").json()["actions"]["blocked"]
    n = client.post(f"/internal/ebay-cases/{a}/notes", json={"text": "11.14到期"}).json()
    assert n["text"] == "11.14到期" and n["created_by"] == "tester"
    assert [x["text"] for x in client.get(f"/internal/ebay-cases/{a}/full").json()["notes"]] == ["11.14到期"]
    assert client.post(f"/internal/ebay-cases/{b}/inquiry/message", json={"text": "hi"}).status_code == 403
    make_user("cs_ia", "CS", stores=["IA"])
    cs = TestClient(client.app)
    login(cs, "cs_ia")
    can = cs.get(f"/internal/ebay-cases/{a}/full").json()["actions"]["can"]
    assert can == {"shipment": True, "escalate": False, "refund": False, "message": True}
    assert cs.get(f"/internal/ebay-cases/{b}/full").status_code == 404  # 不是负责的店铺
    r = cs.post(f"/internal/ebay-cases/{a}/inquiry/refund", json={"amount": 146.57, "currency": "GBP", "confirm": True})
    assert r.status_code == 403
