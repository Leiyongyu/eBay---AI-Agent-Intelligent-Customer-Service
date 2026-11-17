"""退货 / 纠纷处理页：按 eBay 给卖家的下一步决定能做什么、接受退货四种方式、全额 / 部分退款的金额核对、
上传文件、发信走发件箱（到期发到 Post-Order send_message）、做成后写会话事件 / 售后状态 / 状态卡、纠纷结果和申诉。
eBay 全部用假接口（httpx.MockTransport / 假只读 API），不联网、不调模型。"""
import copy
import json
from datetime import datetime, timedelta

import httpx
import pytest

from conftest import make_user

RETURN = {
    "summary": {
        "returnId": "5300001", "orderId": "26-12345-67890", "buyerLoginName": "buyer_r", "state": "RETURN_REQUESTED",
        "status": "WAITING_FOR_RETURN_LABEL", "currentType": "MONEY_BACK",
        "creationInfo": {"reason": "NO_LONGER_NEED_ITEM", "comments": {"content": "Wrong size"},
                         "creationDate": {"value": "2026-11-10T08:00:00.000Z"}},
        "sellerResponseDue": {"activityDue": "SELLER_APPROVE_REQUEST", "respondByDate": {"value": "2026-11-13T08:00:00.000Z"}},
        "sellerAvailableOptions": [{"actionType": "SELLER_APPROVE_REQUEST"}, {"actionType": "SELLER_DECLINE_REQUEST"},
                                   {"actionType": "SELLER_SEND_MESSAGE"}],
        "returnPolicy": {"rmaRequired": False},
    },
    "detail": {
        "refundInfo": {"estimatedRefundDetail": {"itemizedRefundDetails": [
            {"refundFeeType": "PURCHASE_PRICE", "estimatedAmount": {"value": 40.0, "currency": "GBP"},
             "minAmount": {"value": 20.0, "currency": "GBP"}, "maxAmount": {"value": 40.0, "currency": "GBP"}},
            {"refundFeeType": "ORIGINAL_SHIPPING", "estimatedAmount": {"value": 5.0, "currency": "GBP"},
             "minAmount": {"value": 5.0, "currency": "GBP"}, "maxAmount": {"value": 5.0, "currency": "GBP"}}]}},
        "responseHistory": [{"author": "BUYER", "activity": "BUYER_CREATE_RETURN",
                             "creationDate": {"value": "2026-11-10T08:00:00.000Z"}, "notes": "Wrong size"}],
        "returnShipmentInfo": {"shipmentTracking": {"carrierName": "Royal Mail", "trackingNumber": "RM1", "deliveryStatus": "CREATED"}},
    },
}


def with_options(*opts, **summary):
    d = copy.deepcopy(RETURN)
    d["summary"]["sellerAvailableOptions"] = [{"actionType": o} for o in opts]
    d["summary"].update(summary)
    return d


CASE = {"caseId": "5100001", "status": "CLOSED", "caseType": "RETURN", "returnId": "5300001",
        "claimAmount": {"value": 45.0, "currency": "GBP"}, "sellerClosureReason": "SELLER_WINS_BUYER_FAULT",
        "appealDetails": {"eligibleForAppeal": False},
        "caseDetails": {"creationDate": {"value": "2026-11-01T00:00:00.000Z"}},
        "caseHistoryDetails": {"history": [{"actor": "CSR", "action": "DECIDE", "description": "Seller wins",
                                            "date": {"value": "2026-11-05T00:00:00.000Z"}}]}}


class FakeApi:
    """假只读 API：按路径返回退货 / 纠纷详情。"""

    def __init__(self, ret=None, case=None):
        self.ret, self.case, self.paths = ret or RETURN, case or CASE, []

    def get(self, path, params=None, auth="Bearer", marketplace=None):
        assert auth == "IAF"
        self.paths.append(path)
        return self.case if "/casemanagement/" in path else self.ret


class FakeEbay:
    def __init__(self, fail_on=None, status=400, file_id="F-1"):
        self.requests: list[httpx.Request] = []
        self.fail_on, self.status, self.file_id = fail_on, status, file_id

    def __call__(self, request):
        self.requests.append(request)
        if self.fail_on and request.url.path.endswith(self.fail_on):
            return httpx.Response(self.status, json={"error": [{"message": "Label not allowed"}]})
        if request.url.path.endswith("/file/upload"):
            return httpx.Response(200, json={"fileId": self.file_id})
        return httpx.Response(200, json={})

    def paths(self):
        return [r.url.path.rsplit("/", 1)[-1] if "file" not in r.url.path else "file/upload" for r in self.requests]

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture()
def env(app_env, monkeypatch):
    from app import case_actions_common as common
    from app import config
    from app.db import session_scope
    from app.models import Conversation, EbayCase, Store

    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "店铺R")
    config.get_settings.cache_clear()
    monkeypatch.setattr("app.ebay.auth.EbayTokenManager.access_token", lambda self: "AT")
    monkeypatch.setattr(common, "order_refund_state", lambda store, oid: {"payment_status": "PAID", "refunds": []})
    monkeypatch.setattr(common, "refresh_reasons", lambda store_id: None)
    with session_scope() as db:
        db.add(Store(id="SR", name="店铺R", default_marketplace_id="EBAY_GB"))
        db.add(Store(id="SX", name="店铺X", default_marketplace_id="EBAY_GB"))
        db.flush()
        conv = Conversation(store_id="SR", ebay_conversation_id="C-R1", buyer_key="buyer_r", channel_source="ebay",
                            marketplace_id="EBAY_GB", last_message_at=datetime(2026, 11, 10))
        db.add(conv)
        r = EbayCase(store_id="SR", kind="RETURN", external_id="5300001", order_id="26-12345-67890", buyer="buyer_r",
                     status="RETURN_REQUESTED/WAITING", is_open=True, amount=45.0, currency="GBP")
        x = EbayCase(store_id="SX", kind="RETURN", external_id="5300009", buyer="someone", status="OPEN", is_open=True)
        k = EbayCase(store_id="SR", kind="CASE", external_id="5100001", buyer="buyer_r", status="OPEN", is_open=True,
                     amount=45.0, currency="GBP")
        db.add_all([r, x, k])
        db.flush()
        ids = {"ret": r.id, "other": x.id, "case": k.id, "conv": conv.id}
    yield ids
    config.get_settings.cache_clear()


class U:
    def __init__(self, role):
        self.role, self.display_name = role, f"u_{role}"


def _run(mod, cid, action, payload, role="AFTERSALES", fake=None, api=None):
    from app.db import session_scope
    from app.models import EbayCase

    fake = fake or FakeEbay()
    with session_scope() as db:
        c = db.get(EbayCase, cid)
        r = mod.perform(db, c, action, payload, U(role), http=httpx.Client(transport=httpx.MockTransport(fake)),
                        api=api or FakeApi())
    return r, fake


def _exec(cid, payload, fake=None, api=None, return_id=None):
    """模拟 2 号的退款申请单审核通过后调执行函数（申请单用简单对象代替）。"""
    from types import SimpleNamespace

    from app import return_actions as ra
    from app.db import session_scope

    fake = fake or FakeEbay()
    mode = payload.get("mode", "full")
    rr = SimpleNamespace(id=7, case_id=cid, return_id=return_id, full=mode == "full", amount=payload["amount"],
                         currency=payload.get("currency", "GBP"), comment=payload.get("comment"),
                         return_payload={"mode": mode, "keep_item": payload.get("keep_item", True)})
    with session_scope() as db:
        r = ra.execute_refund_request(db, rr, U("AFTERSALES"), http=httpx.Client(transport=httpx.MockTransport(fake)),
                                      api=api or FakeApi())
    return r, fake


def _get(model, i):
    from app.db import session_scope

    with session_scope() as db:
        o = db.get(model, i)
        db.expunge(o)
        return o


# ------------------------------------------------------------------ 解析
def test_parse_return_detail_options_and_ranges():
    from app.return_actions import can_do, parse_detail

    d = parse_detail(RETURN)
    assert d["options"][0] == "SELLER_APPROVE_REQUEST" and d["option_labels"][0] == "同意退货"
    assert d["estimated_refund"] == {"value": 45.0, "currency": "GBP"} and d["partial_refund_allowed"] is True
    assert [x["fee_type"] for x in d["refund_lines"]] == ["PURCHASE_PRICE", "ORIGINAL_SHIPPING"]
    assert d["history"][0]["description"] == "Wrong size" and d["tracking"]["number"] == "RM1"
    assert can_do(d) == {"accept": True, "refund": False, "received": False, "file": True, "message": True}
    no_partial = copy.deepcopy(RETURN)
    no_partial["detail"]["refundInfo"]["estimatedRefundDetail"]["itemizedRefundDetails"][0]["minAmount"]["value"] = 40.0
    assert parse_detail(no_partial)["partial_refund_allowed"] is False


# ------------------------------------------------------------------ 接受退货
def test_accept_upload_label_three_calls(env):
    from app import return_actions as ra
    from app.models import EbayCase

    payload = {"method": "upload_label", "rma": "RMA-9", "tracking_number": "RL123GB", "carrier": "Royal Mail",
               "file_data": "data:application/pdf;base64,JVBERi0xLjQK", "file_name": "label.pdf", "comment": "Please use this label"}
    r, fake = _run(ra, env["ret"], "accept", payload)
    assert fake.paths() == ["decide", "file/upload", "add_shipping_label"]
    assert fake.body(0) == {"decision": "APPROVE", "RMANumber": "RMA-9", "comments": {"content": "Please use this label"}}
    assert fake.body(1) == {"data": "JVBERi0xLjQK", "fileName": "label.pdf", "filePurpose": "LABEL_RELATED"}
    assert fake.body(2) == {"labelAction": "UPLOAD_LABEL", "trackingNumber": "RL123GB", "carrierName": "Royal Mail",
                            "comments": {"content": "Please use this label"}, "fileId": "F-1"}
    assert fake.requests[0].headers["Authorization"] == "IAF AT"
    assert r["done"] == "同意退货，RMA RMA-9，提供退货面单 RL123GB（Royal Mail）" and "partial_error" not in r
    assert _get(EbayCase, env["ret"]).work_state == "PROCESSING"


def test_accept_other_methods_and_validation(env):
    from app import return_actions as ra

    _, fake = _run(ra, env["ret"], "accept", {"method": "no_label", "no_label_reason": "ITEM_TOO_LARGE",
                                              "comment": "Item is 30kg, we will collect it"})
    assert fake.body(-1) == {"labelAction": "NO_LABEL", "noLabelReason": "ITEM_TOO_LARGE",
                             "comments": {"content": "Item is 30kg, we will collect it"}}
    _, fake = _run(ra, env["ret"], "accept", {"method": "ebay_label", "carrier_enum": "ROYAL_MAIL"})
    assert fake.body(-1) == {"labelAction": "EBAY_LABEL", "carrierEnum": "ROYAL_MAIL"}
    _, fake = _run(ra, env["ret"], "accept", {"method": "accept_only"})
    assert fake.paths() == ["decide"] and fake.body() == {"decision": "APPROVE"}
    for bad in ({"method": "no_label", "no_label_reason": "ITEM_TOO_LARGE"},  # 留言必填
                {"method": "no_label", "no_label_reason": "NOPE", "comment": "x"},
                {"method": "upload_label", "tracking_number": "1", "carrier": "RM", "file_data": "aGk=", "file_name": "a.exe"},
                {"method": "ebay_label", "carrier_enum": "PIGEON"}, {"method": "teleport"}):
        fake = FakeEbay()
        with pytest.raises(ra.ActionError) as e:
            _run(ra, env["ret"], "accept", bad, fake=fake)
        assert e.value.status_code == 422 and not fake.requests  # 参数先全部校验完才调 eBay
    rma = with_options("SELLER_APPROVE_REQUEST")
    rma["summary"]["returnPolicy"] = {"rmaRequired": True}
    with pytest.raises(ra.ActionError) as e:
        _run(ra, env["ret"], "accept", {"method": "accept_only"}, api=FakeApi(rma))
    assert "RMA" in e.value.message


def test_accept_follows_ebay_options_and_partial_failure(env):
    from sqlalchemy import select

    from app import return_actions as ra
    from app.db import session_scope
    from app.models import EbayCaseNote

    fake = FakeEbay()
    with pytest.raises(ra.ActionError) as e:  # eBay 现在只等买家：不能接受
        _run(ra, env["ret"], "accept", {"method": "accept_only"}, fake=fake, api=FakeApi(with_options("SELLER_SEND_MESSAGE")))
    assert e.value.status_code == 409 and "eBay 给卖家的下一步：给买家留言" in e.value.message and not fake.requests
    # 已经同意过、只差标签：不再调 decide
    _, fake = _run(ra, env["ret"], "accept", {"method": "no_label", "no_label_reason": "REIMBURSE", "comment": "We refund postage"},
                   api=FakeApi(with_options("SELLER_PROVIDE_LABEL")))
    assert fake.paths() == ["add_shipping_label"]
    # 同意成功、面单失败：同意照样记下，返回 partial_error
    r, fake = _run(ra, env["ret"], "accept", {"method": "ebay_label", "carrier_enum": "ROYAL_MAIL"},
                   fake=FakeEbay(fail_on="add_shipping_label"))
    assert fake.paths() == ["decide", "add_shipping_label"] and r["done"] == "同意退货"
    assert "使用 eBay 运输标签（ROYAL_MAIL）没有完成" in r["partial_error"] and "Label not allowed" in r["partial_error"]
    with session_scope() as db:
        assert db.scalars(select(EbayCaseNote.text).order_by(EbayCaseNote.id.desc())).first() == "[接受退货] 同意退货"


def test_senior_actions_and_store_scope(env):
    from app import return_actions as ra

    fake = FakeEbay()
    with pytest.raises(ra.ActionError) as e:
        _run(ra, env["ret"], "accept", {"method": "accept_only"}, role="CS", fake=fake)
    assert e.value.status_code == 403 and not fake.requests
    for role in ("CS", "ADMIN"):  # 退款不在这里直接做：都要提交退款申请
        with pytest.raises(ra.ActionError) as e:
            _run(ra, env["ret"], "refund", {"mode": "full", "amount": 45, "confirm": True}, role=role, fake=fake)
        assert e.value.status_code == 409 and "退款申请" in e.value.message and not fake.requests
    fake = FakeEbay()
    with pytest.raises(ra.ActionError) as e:
        _run(ra, env["other"], "file", {"file_data": "aGk=", "file_name": "a.jpg"}, role="ADMIN", fake=fake)
    assert e.value.status_code == 403 and "eBay 后台" in e.value.message and not fake.requests
    _, fake = _run(ra, env["ret"], "file", {"file_data": "aGk=", "file_name": "photo.jpg", "purpose": "ITEM_RELATED"}, role="CS")
    assert fake.body() == {"data": "aGk=", "fileName": "photo.jpg", "filePurpose": "ITEM_RELATED"}


# ------------------------------------------------------------------ 退款
def test_refund_full_and_partial_within_ebay_range(env):
    from app import return_actions as ra
    from app.models import Conversation, EbayCase

    api = FakeApi(with_options("SELLER_ISSUE_REFUND"))
    fake = FakeEbay()
    with pytest.raises(ra.ActionError) as e:  # 执行时核对：全额对不上 eBay 估算
        _exec(env["ret"], {"mode": "full", "amount": 40}, fake=fake, api=api)
    assert e.value.status_code == 409 and "45.00" in e.value.message and not fake.requests and not e.value.called_ebay
    for amt in (10, 45, 50):  # 低于 eBay 允许的最低（20 + 运费 5）/ 等于全额 / 超过：409，申请单留在待审核
        with pytest.raises(ra.ActionError) as e:
            _exec(env["ret"], {"mode": "partial", "amount": amt}, fake=fake, api=api)
        assert e.value.status_code == 409 and "25.00–45.00" in e.value.message and not e.value.called_ebay
    assert not fake.requests
    r, fake = _exec(env["ret"], {"mode": "partial", "amount": 30, "comment": "Item returned used"}, api=api)
    assert fake.paths() == ["issue_refund"]
    assert fake.body() == {"refundDetail": {"itemizedRefundDetail": [
        {"refundFeeType": "PURCHASE_PRICE", "refundAmount": {"value": 25.0, "currency": "GBP"}},
        {"refundFeeType": "ORIGINAL_SHIPPING", "refundAmount": {"value": 5.0, "currency": "GBP"}}],
        "totalAmount": {"value": 30.0, "currency": "GBP"}}, "comments": {"content": "Item returned used"}}
    assert r["summary"] == "部分退款 GBP 30.00（全额 45.00）（退款申请 #7）" and r["state"] == "pending"
    assert r["return_id"] == "5300001" and _get(EbayCase, env["ret"]).work_state == "DONE"
    conv = _get(Conversation, env["conv"])
    assert conv.aftersales_status == "DONE" and "部分退款 GBP 30.00" in conv.aftersales_note

    class Ok(FakeEbay):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(200, json={"refundStatus": "SUCCESS"})
    r, fake = _exec(env["ret"], {"mode": "full", "amount": 45}, fake=Ok(), api=api)
    assert fake.body()["refundDetail"]["totalAmount"] == {"value": 45.0, "currency": "GBP"} and r["state"] == "refunded"
    with pytest.raises(ra.ActionError) as e:  # eBay 拒绝：called_ebay=True，申请单那边记“处理异常”
        _exec(env["ret"], {"mode": "full", "amount": 45}, fake=FakeEbay(fail_on="issue_refund"), api=api)
    assert e.value.status_code == 422 and e.value.called_ebay and "Label not allowed" in e.value.message


def test_refund_blocked_by_ebay_state(env, monkeypatch):
    from app import case_actions_common as common
    from app import return_actions as ra

    fake = FakeEbay()
    pay = {"mode": "full", "amount": 45, "currency": "GBP", "confirm": True}
    with pytest.raises(ra.ActionError) as e:  # eBay 没给退款选项
        _exec(env["ret"], pay, fake=fake)
    assert e.value.status_code == 409 and "不允许退款" in e.value.message
    fixed = with_options("SELLER_ISSUE_REFUND")
    fixed["detail"]["refundInfo"]["estimatedRefundDetail"]["itemizedRefundDetails"][0]["minAmount"]["value"] = 40.0
    with pytest.raises(ra.ActionError) as e:  # min = max：只能全额
        _exec(env["ret"], {**pay, "mode": "partial", "amount": 30}, fake=fake, api=FakeApi(fixed))
    assert e.value.status_code == 409 and "只能全额" in e.value.message
    done = with_options("SELLER_ISSUE_REFUND")
    done["detail"]["refundInfo"]["actualRefundDetail"] = {"actualRefund": {"totalAmount": {"value": 45, "currency": "GBP"}}}
    with pytest.raises(ra.ActionError) as e:
        _exec(env["ret"], pay, fake=fake, api=FakeApi(done))
    assert "已经退过款" in e.value.message
    monkeypatch.setattr(common, "order_refund_state", lambda s, o: {"payment_status": "FULLY_REFUNDED", "refunds": []})
    with pytest.raises(ra.ActionError) as e:
        _exec(env["ret"], pay, fake=fake, api=FakeApi(with_options("SELLER_ISSUE_REFUND")))
    assert e.value.status_code == 409 and "全额退款" in e.value.message

    def boom(s, o):
        raise RuntimeError("timeout")
    monkeypatch.setattr(common, "order_refund_state", boom)
    with pytest.raises(ra.ActionError) as e:  # 查不到订单退款情况：执行前挡下，409（不是“处理异常”）
        _exec(env["ret"], pay, fake=fake, api=FakeApi(with_options("SELLER_ISSUE_REFUND")))
    assert e.value.status_code == 409 and "查不到订单" in e.value.message and not e.value.called_ebay
    assert not fake.requests


def test_offer_partial_refund_when_only_offer_allowed(env):
    from app import return_actions as ra
    from app.models import EbayCase

    r, fake = _exec(env["ret"], {"mode": "partial", "amount": 15, "keep_item": True},
                    api=FakeApi(with_options("SELLER_OFFER_PARTIAL_REFUND")))
    assert fake.paths() == ["decide"]
    assert fake.body() == {"decision": "OFFER_PARTIAL_REFUND", "partialRefundAmount": {"value": 15.0, "currency": "GBP"},
                           "keepOriginalItem": True}
    assert r["summary"] == "已向买家提出部分退款 GBP 15.00，等买家同意（买家不用退回商品）（退款申请 #7）" and r["state"] == "offered"
    assert _get(EbayCase, env["ret"]).work_state == "PROCESSING"  # 提议不是退款：不算已处理
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Conversation, ConversationEvent

    with session_scope() as db:  # 会话事件和售后状态也如实写“提出、等同意”，不写成已退款
        ev = db.scalars(select(ConversationEvent).where(ConversationEvent.conversation_id == env["conv"])).one()
        assert "已向买家提出部分退款 GBP 15.00，等买家同意" in ev.detail and "已退款" not in ev.detail
        assert db.get(Conversation, env["conv"]).aftersales_status == "IN_PROGRESS"


def test_timeout_not_retried(env):
    from app import return_actions as ra

    class Boom(FakeEbay):
        def __call__(self, request):
            self.requests.append(request)
            raise httpx.ReadTimeout("t")

    fake = Boom()
    with pytest.raises(ra.ActionError) as e:
        _run(ra, env["ret"], "accept", {"method": "accept_only"}, fake=fake)
    assert e.value.status_code == 502 and "不确定" in e.value.message and len(fake.requests) == 1


# ------------------------------------------------------------------ 会话：系统事件、状态卡、发信走发件箱
def test_action_writes_conversation_event_and_state_card(env):
    from sqlalchemy import select

    from app import conversation_state
    from app import return_actions as ra
    from app.db import session_scope
    from app.models import AuditEvent, Conversation, ConversationEvent

    before = _get(Conversation, env["conv"]).row_version
    _run(ra, env["ret"], "accept", {"method": "no_label", "no_label_reason": "WORK_WITH_BUYER", "comment": "We will arrange"})
    with session_scope() as db:
        ev = db.scalars(select(ConversationEvent).where(ConversationEvent.conversation_id == env["conv"])).one()
        assert ev.source == "case_action" and ev.event_type == "SELLER_RETURN_ACCEPT" and ev.case_id == "5300001"
        assert "不提供退货标签：和买家商量退货运费" in ev.detail
        assert db.scalar(select(AuditEvent.action).where(AuditEvent.action == "RETURN_ACCEPT"))
        conv = db.get(Conversation, env["conv"])
        assert conv.row_version == before + 1 and conv.aftersales_status == "IN_PROGRESS" and conv.aftersales_type == "退货"
        card = conversation_state.current(db, conv)
        text = conversation_state.render(card)
        assert "已在 eBay 上做的处理（系统记录）：" in text and "退货 5300001：同意退货" in text
    from app.prompts import load_prompt

    assert "已在 eBay 上做的处理（系统记录）" in load_prompt("reply")


def test_message_goes_through_outbox_then_post_order(env, monkeypatch):
    from sqlalchemy import select

    from app import case_actions_common as common
    from app import config
    from app import return_actions as ra
    from app.db import session_scope
    from app.models import EbayCaseNote, Message, OutboundMessage
    from app.sending import outbox

    monkeypatch.setenv("SEND_DELAY_SECONDS", "300")
    config.get_settings.cache_clear()
    fake = FakeEbay()
    r, _ = _run(ra, env["ret"], "message", {"text": "Hello, please send it back with the label."}, role="CS", fake=fake)
    assert not fake.requests and r["queued"]["state"] == "PENDING"  # 先进发件箱，不直接发
    with session_scope() as db:
        ob = db.get(OutboundMessage, r["queued"]["outbound_id"])
        assert ob.conversation_id == env["conv"] and ob.meta["channel"] == "post_order"
        assert ob.meta["post_order_path"] == "/post-order/v2/return/5300001/send_message" and ob.meta["case_id"] == env["ret"]
    with pytest.raises(ra.ActionError) as e:  # 合规硬阻断：和工作台一样不能发
        _run(ra, env["ret"], "message", {"text": "Please leave positive feedback and we will refund you"}, role="CS")
    assert e.value.status_code == 422
    sent = []
    monkeypatch.setattr(common, "post", lambda store, path, body, http=None: sent.append((path, body)) or {})
    with session_scope() as db:  # 到期：发件箱改调 Post-Order send_message
        ob = db.get(OutboundMessage, r["queued"]["outbound_id"])
        ob.send_after = ob.send_after - timedelta(minutes=10)
    outbox.dispatch_due()
    assert sent == [("/post-order/v2/return/5300001/send_message", {"message": {"content": "Hello, please send it back with the label."}})]
    with session_scope() as db:
        ob = db.get(OutboundMessage, r["queued"]["outbound_id"])
        assert ob.state == "SENT" and ob.channel == "post_order" and ob.platform_message_id.startswith("PO-RET-")
        assert db.get(Message, ob.message_id).body.startswith("Hello")
        assert any(n.startswith("[给买家发信] 已发到 eBay") for n in db.scalars(select(EbayCaseNote.text)))


def test_message_uncertain_and_no_conversation(env, monkeypatch):
    from app import case_actions_common as common
    from app import return_actions as ra
    from app.db import session_scope
    from app.models import EbayCase, OutboundMessage

    def boom(store, path, body, http=None):
        raise common.ActionError(502, "请求 eBay 出错（ReadTimeout），不确定是否已处理，请到 eBay 后台核对")
    monkeypatch.setattr(common, "post", boom)
    r, _ = _run(ra, env["ret"], "message", {"text": "Hi"}, role="CS")  # 延迟 0：提交即发
    with session_scope() as db:
        assert db.get(OutboundMessage, r["queued"]["outbound_id"]).state == "UNCERTAIN"  # 不确定：不重发
        db.get(EbayCase, env["ret"]).buyer = "nobody"
    with pytest.raises(ra.ActionError) as e:
        _run(ra, env["ret"], "message", {"text": "Hi"}, role="CS")
    assert e.value.status_code == 409 and "对不上买家会话" in e.value.message


# ------------------------------------------------------------------ 纠纷
def test_case_outcome_mapping():
    from app.case_actions import outcome

    assert outcome(None, "SELLER_WINS_BUYER_FAULT") == "SELLER"
    assert outcome("AUTO_DISPOSE_BUYER_WIN_NO_SELLER_FAULT", None) == "BUYER"
    assert outcome("BUYER_CONFIRM_FULL_REFUND_PP", None) == "BUYER" and outcome(None, "SELLER_FULL_REFUND_PP") == "BUYER"
    assert outcome("BUYER_CLOSE", None) is None and outcome(None, None) is None


def test_case_actions_forward_to_return_and_appeal(env):
    from app import case_actions as ca
    from app.models import EbayCase

    fake = FakeEbay()
    with pytest.raises(ca.ActionError) as e:  # 不能申诉
        _run(ca, env["case"], "appeal", {"comment": "Tracking shows delivered"}, fake=fake)
    assert e.value.status_code == 409 and "不能申诉" in e.value.message and not fake.requests
    assert _get(EbayCase, env["case"]).detail is None  # 处理失败整体回滚（结果由详情页和同步补查记下）
    inr = {**CASE, "caseType": "INR", "returnId": None, "status": "OPEN", "sellerClosureReason": None}
    with pytest.raises(ca.ActionError) as e:  # 未收到货纠纷：eBay 没有退款接口
        _run(ca, env["case"], "refund", {"mode": "full", "amount": 45, "confirm": True}, fake=fake, api=FakeApi(case=inr))
    assert "只能查询和申诉" in e.value.message and not fake.requests
    appealable = {**inr, "appealDetails": {"eligibleForAppeal": True}}
    r, fake = _run(ca, env["case"], "appeal", {"comment": "Tracking shows delivered"}, api=FakeApi(case=appealable))
    assert fake.requests[0].url.path == "/post-order/v2/casemanagement/5100001/appeal"
    assert fake.body() == {"comments": {"content": "Tracking shows delivered"}} and r["done"] == "已提交申诉"
    with pytest.raises(ca.ActionError) as e:
        _run(ca, env["case"], "appeal", {"comment": "x"}, role="CS", api=FakeApi(case=appealable))
    assert e.value.status_code == 403
    # 退货升级来的纠纷：退款也要提交退款申请；审核通过后按那条退货（getCase 的 returnId）退
    api = FakeApi(ret=with_options("SELLER_ISSUE_REFUND"), case={**CASE, "status": "OPEN"})
    with pytest.raises(ca.ActionError) as e:
        _run(ca, env["case"], "refund", {"mode": "full", "amount": 45, "currency": "GBP", "confirm": True}, api=api)
    assert e.value.status_code == 409 and "退款申请" in e.value.message
    r, fake = _exec(env["case"], {"mode": "full", "amount": 45}, api=api)
    assert fake.requests[0].url.path == "/post-order/v2/return/5300001/issue_refund" and r["summary"].startswith("全额退款")


def test_case_refund_checks_order_refunded(env, monkeypatch):
    """D1：纠纷转到退货退款时也核对订单（用退货上的订单号；纠纷记录上是 商品号-交易号）。"""
    from app import case_actions_common as common

    seen = []
    monkeypatch.setattr(common, "order_refund_state",
                        lambda s, o: seen.append(o) or {"payment_status": "FULLY_REFUNDED", "refunds": []})
    fake = FakeEbay()
    with pytest.raises(common.ActionError) as e:
        _exec(env["case"], {"mode": "full", "amount": 45}, fake=fake,
              api=FakeApi(ret=with_options("SELLER_ISSUE_REFUND"), case={**CASE, "status": "OPEN"}))
    assert e.value.status_code == 409 and "全额退款" in e.value.message and not fake.requests
    assert seen == ["26-12345-67890"]


def test_failed_call_still_audited(env, client, monkeypatch):
    """D2：调 eBay 超时 / 5xx / 被拒时处理回滚，但另外记 {类型}_{处理}_FAILED 审计和一条处理备注。"""
    from sqlalchemy import select

    from app import case_actions_common as common
    from app import return_actions as ra
    from app.db import session_scope
    from app.models import AuditEvent, EbayCaseNote

    def timeout(store, path, body, http=None):
        e = common.ActionError(502, "请求 eBay 出错（ReadTimeout），不确定是否已处理，请到 eBay 后台核对")
        e.called_ebay, e.endpoint = True, path
        raise e
    monkeypatch.setattr(common, "post", timeout)
    monkeypatch.setattr(ra, "live_detail", lambda store, rid, api_=None: ra.parse_detail(RETURN))
    r = client.post(f"/internal/ebay-cases/{env['ret']}/return/accept", json={"method": "accept_only"})
    assert r.status_code == 502
    with session_scope() as db:
        ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "RETURN_ACCEPT_FAILED")).one()
        assert ev.detail["uncertain"] is True and ev.detail["endpoint"] == "/post-order/v2/return/5300001/decide"
        assert db.scalars(select(EbayCaseNote.text)).one().startswith("[结果不确定] accept")
        assert not db.scalars(select(AuditEvent).where(AuditEvent.action == "RETURN_ACCEPT")).all()


def test_fill_outcomes_and_list_filter(env, client):
    from app import case_actions as ca
    from app.db import session_scope
    from app.models import EbayCase

    with session_scope() as db:
        db.get(EbayCase, env["case"]).is_open = False
        assert ca.fill_outcomes(db, "SR", api=FakeApi()) == 1
        assert ca.fill_outcomes(db, "SR", api=FakeApi()) == 0  # 查过的不再查
    rows = client.get("/internal/ebay-cases", params={"kind": "CASE", "outcome": "SELLER"}).json()
    assert [x["external_id"] for x in rows] == ["5100001"] and rows[0]["outcome"] == "SELLER"
    assert client.get("/internal/ebay-cases", params={"kind": "CASE", "outcome": "BUYER"}).json() == []
    assert client.get("/internal/ebay-cases", params={"kind": "CASE", "outcome": "X"}).status_code == 422


# ------------------------------------------------------------------ 接口
def test_endpoints_full_and_actions(env, client, monkeypatch):
    from app import case_actions_common as common
    from app import return_actions as ra
    from app.api import ebay_cases as api

    monkeypatch.setattr(ra, "live_detail", lambda store, rid, api_=None: ra.parse_detail(RETURN))
    monkeypatch.setattr(api, "_order_info", lambda store, oid: {"order_id": oid, "packages": []})
    f = client.get(f"/internal/ebay-cases/{env['ret']}/full").json()
    assert f["live"]["return_id"] == "5300001" and f["conversation_id"] == env["conv"]
    assert f["actions"]["can"] == {"accept": True, "refund": False, "received": False, "file": True, "message": True}
    assert "eBay 现在不允许退款" in f["actions"]["why"]["refund"] and f["no_label_reasons"][0]["code"] == "ITEM_TOO_LARGE"
    sent = []
    monkeypatch.setattr(common, "post", lambda store, path, body, http=None: sent.append(path) or {})
    r = client.post(f"/internal/ebay-cases/{env['ret']}/return/accept", json={"method": "accept_only"})
    assert r.status_code == 200 and r.json()["done"] == "同意退货" and sent == ["/post-order/v2/return/5300001/decide"]
    r = client.post(f"/internal/ebay-cases/{env['ret']}/return/refund", json={"mode": "full", "amount": 45, "confirm": True})
    assert r.status_code == 409 and len(sent) == 1
    assert client.post(f"/internal/ebay-cases/{env['other']}/return/accept", json={"method": "accept_only"}).status_code == 403
    make_user("cs_r", "CS", stores=["SR"])
    from fastapi.testclient import TestClient

    from conftest import login

    cs = TestClient(client.app)
    login(cs, "cs_r")
    f = cs.get(f"/internal/ebay-cases/{env['ret']}/full").json()
    assert f["actions"]["can"]["accept"] is False and f["actions"]["senior"]["accept"] is True
    assert cs.post(f"/internal/ebay-cases/{env['ret']}/return/accept", json={"method": "accept_only"}).status_code == 403


def test_case_message_blocked_when_ebay_closed_it(env):
    """eBay 介入后对应退货不开放留言：不进发件箱，原因写明“没有开放接口，请到 eBay 后台”。"""
    from app import case_actions as ca

    fake = FakeEbay()
    with pytest.raises(ca.ActionError) as e:
        _run(ca, env["case"], "message", {"text": "Hi"}, role="CS", fake=fake,
             api=FakeApi(ret=with_options("SELLER_ISSUE_REFUND"), case={**CASE, "status": "OPEN"}))
    assert e.value.status_code == 409 and "没有给卖家开放留言接口" in e.value.message and "eBay 后台" in e.value.message
    assert ca.platform_reason(ca.parse_detail({**CASE, "returnId": None}), None, "refund").endswith("eBay 没有开放接口，请到 eBay 后台处理")


def test_post_signs_exact_bytes_and_explains_signature_errors(env, monkeypatch):
    from app import case_actions_common as common
    from app.ebay import signing

    seen = {}
    monkeypatch.setattr(signing, "sign_headers", lambda m, url, raw: seen.update(raw=raw, url=url) or {"Signature": "sig1=:x:",
                                                                                                         "Content-Digest": "d"})
    fake = FakeEbay()
    common.post("SR", "/post-order/v2/return/1/decide", {"decision": "APPROVE", "comments": {"content": "Grüße"}},
                httpx.Client(transport=httpx.MockTransport(fake)))
    req = fake.requests[0]
    assert req.headers["Signature"] == "sig1=:x:" and req.content == seen["raw"]  # 签的就是发出去的那份字节
    assert seen["raw"] == '{"decision":"APPROVE","comments":{"content":"Grüße"}}'.encode()

    def sig_err(request):
        return httpx.Response(400, json={"errors": [{"errorId": 215122, "message": "Signature validation failed"}]})
    with pytest.raises(common.ActionError) as e:
        common.post("SR", "/post-order/v2/return/1/issue_refund", {}, httpx.Client(transport=httpx.MockTransport(sig_err)))
    assert e.value.status_code == 422 and "数字签名" in e.value.message
    assert e.value.signature_code == "215122" and e.value.endpoint == "/post-order/v2/return/1/issue_refund"


def test_signature_error_recorded_by_endpoint(env, client, monkeypatch):
    """处理接口遇到签名错误：回滚处理，另外记一条 EBAY_SIGNATURE_ERROR 审计（带错误码、接口）。"""
    from sqlalchemy import select

    from app import case_actions_common as common
    from app.db import session_scope
    from app.models import AuditEvent

    def refuse(store, path, body, http=None):
        e = common.ActionError(422, "eBay 拒绝了数字签名")
        e.signature_code, e.endpoint = "215120", path
        raise e
    monkeypatch.setattr(common, "post", refuse)
    from app import return_actions as ra

    monkeypatch.setattr(ra, "live_detail", lambda store, rid, api_=None: ra.parse_detail(RETURN))
    r = client.post(f"/internal/ebay-cases/{env['ret']}/return/accept", json={"method": "accept_only"})
    assert r.status_code == 422 and "数字签名" in r.json()["detail"]
    with session_scope() as db:
        ev = db.scalars(select(AuditEvent).where(AuditEvent.action == "EBAY_SIGNATURE_ERROR")).one()
        assert ev.store_id == "SR" and ev.detail == {"endpoint": "/post-order/v2/return/5300001/decide", "error_code": "215120"}
        assert not db.scalars(select(AuditEvent).where(AuditEvent.action == "RETURN_ACCEPT")).all()


def test_full_gives_ebay_link(env, client, monkeypatch):
    from app import case_actions as ca
    from app import return_actions as ra
    from app.api import ebay_cases as api

    monkeypatch.setattr(api, "_order_info", lambda store, oid: {"order_id": oid, "packages": []})
    monkeypatch.setattr(ra, "live_detail", lambda store, rid, api_=None: ra.parse_detail(RETURN))
    f = client.get(f"/internal/ebay-cases/{env['ret']}/full").json()
    assert f["ebay_url"] == "https://resolutioncenter.ebay.co.uk/"  # eBay 没给链接：店铺站点的纠纷处理中心
    inr = {**CASE, "caseType": "INR", "returnId": None, "buyerProtectedProgramLink": "https://www.ebay.co.uk/rc/123"}
    monkeypatch.setattr(ca, "live_detail", lambda store, cid, api_=None: ca.parse_detail(inr))
    f = client.get(f"/internal/ebay-cases/{env['case']}/full").json()
    assert f["ebay_url"] == "https://www.ebay.co.uk/rc/123"
    assert f["actions"]["why"]["refund"].endswith("eBay 没有开放接口，请到 eBay 后台处理")
    assert f["actions"]["can"]["refund"] is False and f["actions"]["why"]["appeal"] == "eBay 显示这条纠纷现在不能申诉"


def test_refund_request_signal_in_aftersales_reasons(env):
    """2 号的退款申请单（已退 / 退款中）进售后原因：按退款原因归类，退货退款看退货单。"""
    from app.aftersales_reasons import _empty_sig, classify, collect_store
    from app.db import session_scope
    from app.models import EbayOrder, RefundRequest

    assert classify({**_empty_sig(), "refunded": True, "refund_reqs": ["ITEM_NOT_AS_DESCRIBED"]})[0] == "LISTING"
    assert classify({**_empty_sig(), "refunded": True, "refund_reqs": ["SELLER_CANCEL"]})[0] == "WAREHOUSE"
    assert classify({**_empty_sig(), "refunded": True, "refund_reqs": ["BUYER_RETURN"]})[0] is None
    now = datetime.utcnow()
    with session_scope() as db:
        db.add(EbayOrder(store_id="SR", order_id="26-00000-00001", buyer="b1", created_at=now - timedelta(days=3)))
        db.add(EbayOrder(store_id="SR", order_id="26-00000-00002", buyer="b2", created_at=now - timedelta(days=3)))
        for oid, st in (("26-00000-00001", "REFUNDED"), ("26-00000-00002", "PENDING")):
            db.add(RefundRequest(store_id="SR", order_id=oid, source="MANUAL", full=True, amount=10, currency="GBP",
                                 reason="ITEM_NOT_RECEIVED", status=st, submitted_by="t"))
        db.flush()
        _, sigs = collect_store(db, "SR", now)
        assert sigs["26-00000-00001"]["refund_reqs"] == ["ITEM_NOT_RECEIVED"]
        assert sigs["26-00000-00001"]["events"][0]["kind"] == "REFUND_REQUEST"
        assert classify(sigs["26-00000-00001"])[:2] == ("LOGISTICS", "本系统退款申请：没收到货（ITEM_NOT_RECEIVED）")
        assert "26-00000-00002" not in sigs  # 待审核的不算


def test_refund_request_end_to_end_via_return(env, monkeypatch):
    """退货退款走退款申请单：提交（默认先审核）→ 审核通过 → 调 execute_refund_request 用退货接口退 → 申请单退款中、
    售后单已处理、售后原因表标退款（D3）。2 号的申请单逻辑用真的，eBay 用假的。"""
    from app import case_actions_common as common
    from app import refund_requests as rq
    from app import return_actions as ra
    from app.aftersales_reasons import collect_store
    from app.db import session_scope, utcnow
    from app.models import EbayCase, EbayOrder

    monkeypatch.setattr("app.auth.check_store", lambda sid: None)
    monkeypatch.setattr(ra, "live_detail", lambda store, rid, api_=None: ra.parse_detail(with_options("SELLER_ISSUE_REFUND")))
    sent = []
    monkeypatch.setattr(common, "post", lambda store, path, body, http=None: sent.append((path, body)) or {})
    with session_scope() as db:
        db.add(EbayOrder(store_id="SR", order_id="26-12345-67890", buyer="buyer_r", total=45.0, currency="GBP",
                         created_at=utcnow() - timedelta(days=5)))
        db.flush()
        rr = rq.create(db, U("CS"), {"order_id": "26-12345-67890", "case_id": env["ret"], "full": False, "amount": 30.0,
                                     "currency": "GBP", "confirm_amount": 30.0, "confirm_currency": "GBP",
                                     "execution": "post_order_return", "return_payload": {"mode": "partial"}})
        assert rr.status == "PENDING" and rr.execution == "post_order_return" and rr.return_id == "5300001" and not sent
        rq.approve(db, rr, U("AFTERSALES"), {"confirm_amount": 30.0, "confirm_currency": "GBP"})
        assert sent[0][0] == "/post-order/v2/return/5300001/issue_refund"
        assert sent[0][1]["refundDetail"]["totalAmount"] == {"value": 30.0, "currency": "GBP"}
        assert rr.status == "REFUNDING" and db.get(EbayCase, env["ret"]).work_state == "DONE"
        _, sigs = collect_store(db, "SR")
        assert sigs["26-12345-67890"]["refunded"] is True


def test_offer_mode_explicit(env):
    """return_payload.mode=offer：明确只向买家提出部分退款；eBay 不给提议选项时 409。"""
    from app import return_actions as ra

    r, fake = _exec(env["ret"], {"mode": "offer", "amount": 10, "keep_item": False},
                    api=FakeApi(with_options("SELLER_ISSUE_REFUND", "SELLER_OFFER_PARTIAL_REFUND")))
    assert fake.paths() == ["decide"] and fake.body()["keepOriginalItem"] is False and r["state"] == "offered"
    with pytest.raises(ra.ActionError) as e:
        _exec(env["ret"], {"mode": "offer", "amount": 10}, api=FakeApi(with_options("SELLER_ISSUE_REFUND")))
    assert e.value.status_code == 409 and "不允许向买家提出部分退款" in e.value.message
