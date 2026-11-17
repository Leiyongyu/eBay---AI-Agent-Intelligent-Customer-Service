"""退款申请单 + 审核流 + 取消交易（同意 / 拒绝 / 主动取消）：只对接店铺、角色、确认金额、实时核对、超时不重试、
审计、会话系统事件（AI 起草能看到）、和退款流水对上、数字签名。eBay 全部用假接口，不联网、不真实退款。"""
import base64
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import login, make_user


def _now():
    from app.db import utcnow

    return utcnow()

LIVE_ORDER = {"orderId": "11-111-001", "orderPaymentStatus": "PAID",
              "pricingSummary": {"total": {"value": "40.55", "currency": "EUR"}}, "paymentSummary": {"refunds": []},
              "lineItems": [{"lineItemId": "L1", "sku": "SKU-A", "lineItemCost": {"value": "30.55", "currency": "EUR"}},
                            {"lineItemId": "L2", "sku": "SKU-B", "lineItemCost": {"value": "10.00", "currency": "EUR"}}]}


class FakeEbay:
    """记录请求；按顺序返回 responses 里的 (status, json) 或抛异常。"""

    def __init__(self, *responses):
        self.responses = list(responses) or [(200, {"refundId": "5000123", "refundStatus": "PENDING"})]
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        status, body = r
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


@pytest.fixture()
def env(client, monkeypatch):
    import httpx as _httpx

    from app import config, refund_requests as rq
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.models import EbayCase, EbayOrder, EbayOrderLine, Store

    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "退款店")
    config.get_settings.cache_clear()
    monkeypatch.setattr("app.ebay.auth.EbayTokenManager.access_token", lambda self: "AT")
    live = {"order": json.loads(json.dumps(LIVE_ORDER))}
    monkeypatch.setattr(rq, "_live_order", lambda store_id, order_id: live["order"])
    fake = {"ebay": FakeEbay()}
    real_client = _httpx.Client
    monkeypatch.setattr(_httpx, "Client", lambda **kw: real_client(transport=_httpx.MockTransport(lambda r: fake["ebay"](r))))
    t = datetime(2026, 11, 10, 8, 0)
    with session_scope() as db:
        db.add(Store(id="RS", name="退款店", default_marketplace_id="EBAY_DE"))
        db.add(Store(id="RX", name="别的店", default_marketplace_id="EBAY_DE"))
        db.flush()
        for sid, oid, total in (("RS", "11-111-001", 40.55), ("RS", "11-111-002", 20.0), ("RS", "11-111-003", 30.0),
                                ("RX", "22-222-001", 15.0)):
            db.add(EbayOrder(store_id=sid, order_id=oid, buyer="buyer_r", total=total, currency="EUR", refunded=False,
                             cancelled=False, created_at=t))
        db.add(EbayOrderLine(store_id="RS", order_id="11-111-001", line_item_id="L1", line_key="111-L1", item_id="111", sku="SKU-A",
                             price_value="30.55", price_currency="EUR"))
        db.add(EbayOrderLine(store_id="RS", order_id="11-111-001", line_item_id="L2", line_key="111-L2", item_id="112", sku="SKU-B",
                             price_value="10.00", price_currency="EUR"))
        conv = get_or_create_conversation(db, "RS", "C-RF1", "buyer_r", "111", "EBAY_DE", None)
        ret = EbayCase(store_id="RS", kind="RETURN", external_id="5333000001", order_id="11-111-002", buyer="buyer_r",
                       status="OPEN", is_open=True, reason="DEFECTIVE_ITEM", amount=20.0, currency="EUR", opened_at=t)
        can = EbayCase(store_id="RS", kind="CANCEL", external_id="5460000001", order_id="11-111-003", buyer="buyer_r",
                       status="CANCEL_REQUESTED", is_open=True, reason="ORDER_MISTAKE", amount=30.0, currency="EUR", opened_at=t)
        can_x = EbayCase(store_id="RX", kind="CANCEL", external_id="5460000009", order_id="22-222-001", buyer="buyer_x",
                         status="CANCEL_REQUESTED", is_open=True, opened_at=t)
        db.add_all([ret, can, can_x])
        db.flush()
        ids = {"conv": conv.id, "ret": ret.id, "cancel": can.id, "cancel_x": can_x.id}
    make_user("cs_rf", "CS", stores=["RS", "RX"])
    make_user("as_rf", "AFTERSALES", stores=["RS", "RX"])
    cs, af = TestClient(client.app), TestClient(client.app)
    login(cs, "cs_rf")
    login(af, "as_rf")
    yield {"admin": client, "cs": cs, "as": af, "ids": ids, "fake": fake, "live": live, "rq": rq}
    config.get_settings.cache_clear()


def _submit(c, **kw):
    body = {"order_id": "11-111-001", "store_id": "RS", "full": True, "amount": 40.55, "currency": "EUR",
            "reason": "ITEM_NOT_AS_DESCRIBED", "confirm_amount": 40.55, "confirm_currency": "EUR", **kw}
    return c.post("/internal/refund-requests", json=body)


def _events(conv_id):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Conversation, ConversationEvent

    with session_scope() as db:
        evs = [(e.event_type, e.source, e.detail) for e in db.scalars(select(ConversationEvent).where(
            ConversationEvent.conversation_id == conv_id).order_by(ConversationEvent.id))]
        conv = db.get(Conversation, conv_id)
        return evs, (conv.row_version, conv.aftersales_status, conv.aftersales_note)


def test_submit_review_and_issue_refund(env):
    cs, admin, fake, ids = env["cs"], env["admin"], env["fake"], env["ids"]
    # 预填：从会话进来，列出买家的订单；指定订单后给出订单行和可退金额
    pf = cs.get("/internal/refund-requests/prefill", params={"conversation_id": ids["conv"]}).json()
    assert set(pf["orders"]) == {"11-111-001", "11-111-002", "11-111-003"} and pf["order_id"] is None
    pf = cs.get("/internal/refund-requests/prefill", params={"conversation_id": ids["conv"], "order_id": "11-111-001"}).json()
    assert pf["remaining"] == 40.55 and len(pf["lines"]) == 2 and pf["mode"] == "review" and pf["can_submit"]
    assert pf["can_refund_directly"] is False and pf["source"] == "MANUAL"

    # 提交：确认金额对不上 / 超过可退 / 全额但金额不等于总额 → 不收
    assert _submit(cs, confirm_amount=40.5).status_code == 409
    assert _submit(cs, amount=50, confirm_amount=50, full=False).status_code == 409
    assert _submit(cs, amount=30, confirm_amount=30, full=True).status_code == 422
    assert _submit(cs, currency="USD", confirm_currency="USD").status_code == 422
    r = _submit(cs, conversation_id=ids["conv"], comment="Sorry, full refund issued", note="买家说外壳开裂")
    assert r.status_code == 200, r.text
    rr = r.json()
    assert (rr["status"], rr["review_mode"], rr["submitted_by"], rr["source"]) == ("PENDING", "review", "cs_rf", "MANUAL")
    assert fake["ebay"].requests == []  # 先审核：没有调 eBay
    assert _submit(cs).status_code == 409  # 同一订单还有没处理完的申请

    # 审核：客服不能审；确认金额要原样带回
    rid = rr["id"]
    assert cs.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"}).status_code == 403
    assert admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.0, "confirm_currency": "EUR"}).status_code == 409
    assert fake["ebay"].requests == []
    r = env["as"].post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR", "note": "同意"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["ebay_refund_id"], out["reviewed_by"], out["executed_by"]) == ("REFUNDING", "5000123", "as_rf", "as_rf")
    req = fake["ebay"].requests[-1]
    assert req.url.host == "apiz.ebay.com" and req.url.path == "/sell/fulfillment/v1/order/11-111-001/issue_refund"
    assert req.headers["Authorization"] == "Bearer AT" and "x-ebay-signature-key" not in req.headers  # 没配签名就不签
    assert fake["ebay"].body() == {"reasonForRefund": "ITEM_NOT_AS_DESCRIBED", "comment": "Sorry, full refund issued",
                                   "orderLevelRefundAmount": {"value": "40.55", "currency": "EUR"}}
    # 会话系统事件（source=case_action，会话状态卡 / AI 起草看得到）、草稿过期、售后备注
    evs, (ver, st, note) = _events(ids["conv"])
    assert evs[-1][0] == "SELLER_REFUND_ISSUED" and evs[-1][1] == "case_action" and "已退款 EUR 40.55" in evs[-1][2]
    assert st == "IN_PROGRESS" and "已退款 EUR 40.55" in note and ver >= 1
    from app import case_actions_common as cac
    from app.db import session_scope
    from app.models import AuditEvent, Conversation

    with session_scope() as db:
        assert any("已退款 EUR 40.55" in x for x in cac.recent_actions(db, db.get(Conversation, ids["conv"])))
        acts = {a.action for a in db.query(AuditEvent).all()}
    assert {"REFUND_REQUEST_CREATED", "REFUND_REQUEST_APPROVED", "REFUND_ISSUED"} <= acts
    # 审核过的不能再审
    assert admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"}).status_code == 409

    # 列表、数量
    lst = admin.get("/internal/refund-requests").json()
    assert lst["counts"]["REFUNDING"] == 1 and lst["items"][0]["status_label"] == "退款中"
    assert admin.get("/internal/refund-requests", params={"status": "PENDING"}).json()["items"] == []


def test_live_checks_timeout_error_and_resolve(env):
    admin, fake, live = env["admin"], env["fake"], env["live"]
    rid = _submit(env["cs"]).json()["id"]
    # 实时订单显示已全额退款：不调 eBay，申请单还是待审核
    live["order"] = {**LIVE_ORDER, "orderPaymentStatus": "FULLY_REFUNDED"}
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.status_code == 409 and "全额退款" in r.json()["detail"]
    # eBay 上已经退过 10：这次最多 30.55
    live["order"] = {**LIVE_ORDER, "paymentSummary": {"refunds": [{"amount": {"value": "10.00", "currency": "EUR"}, "refundStatus": "REFUNDED"}]}}
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.status_code == 409 and "30.55" in r.json()["detail"]
    assert fake["ebay"].requests == []
    assert admin.get(f"/internal/refund-requests/{rid}").json()["status"] == "PENDING"

    # 超时：只发一次，不重试，记“处理异常”
    live["order"] = LIVE_ORDER
    fake["ebay"] = FakeEbay(httpx.ReadTimeout("timeout"))
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.status_code == 200 and r.json()["status"] == "ERROR" and "不确定是否已退款" in r.json()["error"]
    assert len(fake["ebay"].requests) == 1
    evs, _ = _events(env["ids"]["conv"])
    assert not any(e[0] == "SELLER_REFUND_ISSUED" for e in evs)  # 没退成：不写“已退款”事件
    # 处理异常：核对后“没退” → 退回待审核，再审一次（这次 5xx：同样不重试）
    assert env["cs"].post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "not_refunded"}).status_code == 403
    r = admin.post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "not_refunded", "note": "eBay 后台没看到"})
    assert r.json()["status"] == "PENDING"
    fake["ebay"] = FakeEbay((503, {"errors": []}))
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.json()["status"] == "ERROR" and len(fake["ebay"].requests) == 1
    # 核对后“已退款”（填退款 ID）→ 已退款，写会话事件
    r = admin.post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "refunded", "refund_id": "5000999"})
    assert (r.json()["status"], r.json()["ebay_refund_id"]) == ("REFUNDED", "5000999")


def test_signature_error_reject_and_not_live(env, monkeypatch):
    admin, fake = env["admin"], env["fake"]
    rid = _submit(env["cs"]).json()["id"]
    # eBay 要求数字签名（215001）：转成中文提示
    fake["ebay"] = FakeEbay((403, {"errors": [{"errorId": 215001, "message": "Missing x-ebay-signature-key header"}]}))
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.json()["status"] == "ERROR" and "数字签名" in r.json()["error"] and "EBAY_SIGNING" in r.json()["error"]
    admin.post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "not_refunded"})
    # 审核不通过要写原因
    assert admin.post(f"/internal/refund-requests/{rid}/reject", json={"note": ""}).status_code == 422
    r = admin.post(f"/internal/refund-requests/{rid}/reject", json={"note": "买家已经同意换货"})
    assert (r.json()["status"], r.json()["review_note"]) == ("REJECTED", "买家已经同意换货")
    # 不是对接店铺：不收申请，提示到 eBay 后台；预填里也说明
    r = _submit(env["cs"], store_id="RX", order_id="22-222-001", amount=15.0, confirm_amount=15.0)
    assert r.status_code == 403 and "eBay 后台" in r.json()["detail"]
    pf = env["cs"].get("/internal/refund-requests/prefill", params={"store_id": "RX", "order_id": "22-222-001"}).json()
    assert pf["blocked"] and pf["can_submit"] is False


def test_rules_direct_refund_lines_and_case_link(env, monkeypatch):
    admin, cs, af, fake, ids = env["admin"], env["cs"], env["as"], env["fake"], env["ids"]
    # 规则：只有管理员能改；店铺 RS、EUR、≤ 25 直接退款
    rules = [{"store_id": "RS", "currency": "EUR", "min_amount": None, "max_amount": 25, "mode": "direct", "note": "小额直接退"}]
    assert cs.put("/internal/refund-review-rules", json={"rules": rules}).status_code == 403
    assert admin.put("/internal/refund-review-rules", json={"rules": [{"mode": "auto"}]}).status_code == 422
    r = admin.put("/internal/refund-review-rules", json={"rules": rules})
    assert r.status_code == 200 and r.json()["rules"][0]["mode"] == "direct" and r.json()["dispute_auto_refund"] is False
    from app.db import session_scope

    with session_scope() as db:
        assert env["rq"].decide_mode(db, "RS", 20, "EUR")[0] == "direct"
        assert env["rq"].decide_mode(db, "RS", 30, "EUR")[0] == "review"
        assert env["rq"].decide_mode(db, "RS", 20, "GBP")[0] == "review"
        assert env["rq"].decide_mode(db, "RX", 20, "EUR")[0] == "review"
    # 客服提交命中“直接退款”也要审核（退款只有管理员 / 售后能做）
    r = _submit(cs, full=False, amount=10.0, confirm_amount=10.0, reason="OTHER_ADJUSTMENT",
                lines=[{"line_item_id": "L2", "amount": 10.0}])
    assert r.json()["status"] == "PENDING" and r.json()["review_mode"] == "direct"
    admin.post(f"/internal/refund-requests/{r.json()['id']}/reject", json={"note": "测试"})
    # 订单行金额加起来要等于退款金额；订单行要属于这张订单
    assert _submit(af, full=False, amount=10.0, confirm_amount=10.0, lines=[{"line_item_id": "L2", "amount": 9.0}]).status_code == 422
    assert _submit(af, full=False, amount=10.0, confirm_amount=10.0, lines=[{"line_item_id": "LX", "amount": 10.0}]).status_code == 422
    # 售后提交命中直接退款：马上调 eBay，按订单行退
    r = _submit(af, full=False, amount=10.0, confirm_amount=10.0, reason="SHIPPING_DISCOUNT", lines=[{"line_item_id": "L2", "amount": 10.0}])
    assert r.json()["status"] == "REFUNDING", r.text
    assert fake["ebay"].body() == {"reasonForRefund": "SHIPPING_DISCOUNT",
                                   "refundItems": [{"lineItemId": "L2", "refundAmount": {"value": "10.00", "currency": "EUR"}}]}

    # 从退货发起（纠纷退货）：关联记录写处理备注、标已处理，会话事件按退货写
    from app.models import EbayCase

    pf = cs.get("/internal/refund-requests/prefill", params={"case_id": ids["ret"]}).json()
    assert (pf["source"], pf["reason"], pf["order_id"]) == ("AFTERSALES", "ITEM_NOT_AS_DESCRIBED", "11-111-002")
    # 退货还开着：通过退货本身退（post_order_return），不拦
    assert (pf["execution"], pf["return_id"], pf["blocked"]) == ("post_order_return", "5333000001", None)
    # 但按订单退（不带退货）要拦，避免重复退款
    r = _submit(cs, order_id="11-111-002", amount=20.0, confirm_amount=20.0)
    assert r.status_code == 409 and "重复退款" in r.json()["detail"]
    r = _submit(cs, order_id="11-111-002", case_id=ids["ret"], amount=20.0, confirm_amount=20.0,
                return_payload={"mode": "full", "comment": "refund"})
    assert r.status_code == 200, r.text
    assert (r.json()["execution"], r.json()["return_id"], r.json()["source"]) == ("post_order_return", "5333000001", "AFTERSALES")
    # 1 号的执行函数还没上线时：审核会 409，申请单还是待审核
    import app.return_actions as ra

    calls = []
    monkeypatch.delattr(ra, "execute_refund_request", raising=False)
    rid2 = r.json()["id"]
    assert admin.post(f"/internal/refund-requests/{rid2}/approve", json={"confirm_amount": 20.0, "confirm_currency": "EUR"}).status_code == 409

    def fake_exec(db, rr, user, http=None):  # 1 号 return_actions.execute_refund_request 的约定
        from app import case_actions_common as cac

        calls.append((rr.return_id, rr.amount, rr.return_payload))
        case = db.get(EbayCase, rr.case_id)
        cac.record_action(db, case, action="refund", label="退款", summary="已退款 EUR 20.00（全额）", user=user, work_state="DONE")
        return {"state": "refunded", "refund_id": "RR-1", "summary": "已退款 EUR 20.00（全额）"}

    monkeypatch.setattr(ra, "execute_refund_request", fake_exec, raising=False)
    before = len(env["fake"]["ebay"].requests)
    r = admin.post(f"/internal/refund-requests/{rid2}/approve", json={"confirm_amount": 20.0, "confirm_currency": "EUR"})
    assert (r.json()["status"], r.json()["ebay_refund_id"]) == ("REFUNDED", "RR-1"), r.text
    assert calls == [("5333000001", 20.0, {"mode": "full", "comment": "refund"})]
    assert len(env["fake"]["ebay"].requests) == before  # 不走 issueRefund
    from app.models import EbayCaseNote

    with session_scope() as db:
        ret = db.get(EbayCase, ids["ret"])
        assert ret.work_state == "DONE"
        assert any("已退款 EUR 20.00" in n.text for n in db.query(EbayCaseNote).filter(EbayCaseNote.case_id == ret.id))
    evs, _ = _events(ids["conv"])
    assert evs[-1][0] == "SELLER_RETURN_REFUND" and "已退款 EUR 20.00" in evs[-1][2]
    assert sum(1 for e in evs if e[0] == "SELLER_RETURN_REFUND") == 1  # 事件只写一次（由执行函数写）
    # 执行函数报“不确定”：记处理异常；报 409：保持待审核
    r3 = _submit(af, order_id="11-111-002", case_id=ids["ret"], amount=5.0, confirm_amount=5.0, full=False)
    assert r3.status_code == 409  # 同一订单已退过 20（订单总额 20），不能再退


def test_open_cancel_blocks_order_refund(env):
    r = _submit(env["cs"], order_id="11-111-003", amount=30.0, confirm_amount=30.0)
    assert r.status_code == 409 and "取消交易" in r.json()["detail"] and "自动退款" in r.json()["detail"]


def test_reconcile_with_refund_flow(env):
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.models import RefundRequest

    admin = env["admin"]
    rid = _submit(env["cs"]).json()["id"]
    admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    with session_scope() as db:
        rr = db.get(RefundRequest, rid)
        rr.executed_at = _now() - timedelta(minutes=5)
        # 早于执行时间 1 小时以上的流水不算；之后的这一笔对上
        ec.save(db, "RS", "REFUND", [
            {"external_id": "OLD", "order_id": "11-111-001", "status": "PAYOUT", "is_open": False,
             "opened_at": _now() - timedelta(days=3), "amount": 5, "currency": "USD"},
            {"external_id": "RRP-NEW", "order_id": "11-111-001", "status": "FUNDS_AVAILABLE_FOR_PAYOUT", "is_open": False,
             "opened_at": _now(), "amount": 37.1, "currency": "USD"}])
        assert env["rq"].reconcile(db, "RS") == 1
        rr = db.get(RefundRequest, rid)
        assert rr.status == "REFUNDED" and rr.refund_case_id is not None
    # 退款单页面能看到是谁提交、谁审核的
    rows = {x["external_id"]: x for x in admin.get("/internal/ebay-cases", params={"kind": "REFUND"}).json()}
    assert rows["RRP-NEW"]["refund"]["request"]["submitted_by"] == "cs_rf"
    assert rows["RRP-NEW"]["refund"]["request"]["reviewed_by"] == "tester"


def test_refresh_live_marks_refunded(env):
    admin = env["admin"]
    rid = _submit(env["cs"]).json()["id"]
    admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    env["live"]["order"] = {**LIVE_ORDER, "paymentSummary": {"refunds": [{"refundId": "5000123", "refundStatus": "REFUNDED",
                                                                          "amount": {"value": "40.55", "currency": "EUR"}}]}}
    assert admin.post(f"/internal/refund-requests/{rid}/refresh").json()["status"] == "REFUNDED"


# ------------------------------------------------------------------ 取消交易
def test_cancel_approve_reject_create(env):
    admin, cs, af, ids = env["admin"], env["cs"], env["as"], env["ids"]
    env["fake"]["ebay"] = FakeEbay((200, None))
    fake = env["fake"]
    cid = ids["cancel"]
    # 同意：客服不能做；订单号要确认
    assert cs.post(f"/internal/ebay-cases/{cid}/cancel/approve", json={"confirm_order_id": "11-111-003"}).status_code == 403
    assert af.post(f"/internal/ebay-cases/{cid}/cancel/approve", json={"confirm_order_id": "x"}).status_code == 409
    assert fake["ebay"].requests == []
    r = af.post(f"/internal/ebay-cases/{cid}/cancel/approve", json={"confirm_order_id": "11-111-003"})
    assert r.status_code == 200, r.text
    req = fake["ebay"].requests[-1]
    assert req.url.path == "/post-order/v2/cancellation/5460000001/approve" and req.headers["Authorization"] == "IAF AT"
    assert r.json()["is_open"] is False
    evs, (_, st, note) = _events(ids["conv"])
    assert evs[-1][:2] == ("SELLER_CANCEL_APPROVE", "case_action") and "已同意买家取消订单 11-111-003" in evs[-1][2]
    assert "自动全额退款" in evs[-1][2] and st == "IN_PROGRESS"
    assert af.post(f"/internal/ebay-cases/{cid}/cancel/approve", json={"confirm_order_id": "11-111-003"}).status_code == 409  # 已关闭

    # 拒绝：原因必填（只记在本系统），发货信息带给 eBay；不是对接店铺不能做
    assert cs.post(f"/internal/ebay-cases/{ids['cancel_x']}/cancel/reject", json={"reason": "已发货"}).status_code == 403
    from app.db import session_scope
    from app.models import EbayCase

    with session_scope() as db:
        db.get(EbayCase, cid).is_open = True
    assert cs.post(f"/internal/ebay-cases/{cid}/cancel/reject", json={"reason": " "}).status_code == 422
    r = cs.post(f"/internal/ebay-cases/{cid}/cancel/reject",
                json={"reason": "已经发货", "shipped_at": "2026-11-09T10:00:00Z", "tracking_number": "00340434756793385199"})
    assert r.status_code == 200, r.text
    assert fake["ebay"].body() == {"shipmentDate": {"value": "2026-11-09T10:00:00.000Z"}, "trackingNumber": "00340434756793385199"}
    evs, _ = _events(ids["conv"])
    assert evs[-1][0] == "SELLER_CANCEL_REJECT" and "已经发货" in evs[-1][2]

    # 主动取消：原因只能是卖家可用的 3 个；订单号要确认；只有管理员 / 售后
    body = {"store_id": "RS", "order_id": "11-111-002", "reason": "OUT_OF_STOCK_OR_CANNOT_FULFILL", "confirm_order_id": "11-111-002",
            "conversation_id": ids["conv"]}
    assert cs.post("/internal/order-cancellations", json=body).status_code == 403
    assert admin.post("/internal/order-cancellations", json={**body, "reason": "ORDER_MISTAKE"}).status_code == 422
    assert admin.post("/internal/order-cancellations", json={**body, "confirm_order_id": "x"}).status_code == 409
    env["fake"]["ebay"] = FakeEbay((200, {"cancelId": "5460000077"}))
    r = admin.post("/internal/order-cancellations", json=body)
    assert r.status_code == 200, r.text
    assert env["fake"]["ebay"].body() == {"legacyOrderId": "11-111-002", "cancelReason": "OUT_OF_STOCK_OR_CANNOT_FULFILL", "buyerPaid": True}
    assert r.json()["cancel_id"] == "5460000077"
    with session_scope() as db:
        c = db.get(EbayCase, r.json()["case_id"])
        assert (c.kind, c.external_id, c.work_state) == ("CANCEL", "5460000077", "DONE")
    evs, _ = _events(ids["conv"])
    assert evs[-1][0] == "SELLER_CANCEL_CREATE" and "已主动取消订单 11-111-002" in evs[-1][2]
    # 超时不重试
    env["fake"]["ebay"] = FakeEbay(httpx.ConnectTimeout("t"))
    r = admin.post("/internal/order-cancellations", json={**body, "order_id": "11-111-001", "confirm_order_id": "11-111-001"})
    assert r.status_code == 502 and len(env["fake"]["ebay"].requests) == 1


# ------------------------------------------------------------------ 数字签名
def test_signing_headers_verify():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from app.ebay import signing

    # RFC 9530 / eBay 文档里的例子
    assert signing.content_digest(b'{"hello": "world"}') == "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    body = b'{"reasonForRefund":"BUYER_CANCEL"}'
    h = signing.sign_headers("POST", "https://apiz.ebay.com/sell/fulfillment/v1/order/1/issue_refund", body,
                             private_key=pem, jwe="JWE.TOKEN", created=1700000000)
    assert h["x-ebay-signature-key"] == "JWE.TOKEN" and h["Content-Digest"] == signing.content_digest(body)
    params = '("content-digest" "x-ebay-signature-key" "@method" "@path" "@authority");created=1700000000'
    assert h["Signature-Input"] == f"sig1={params}"
    base = signing.signature_base([("content-digest", h["Content-Digest"]), ("x-ebay-signature-key", "JWE.TOKEN"),
                                   ("@method", "POST"), ("@path", "/sell/fulfillment/v1/order/1/issue_refund"),
                                   ("@authority", "apiz.ebay.com")], params)
    sig = base64.b64decode(h["Signature"][len("sig1=:"):-1])
    key.public_key().verify(sig, base.encode())  # 不抛 = 签名对
    # 没有请求体：不带 content-digest；base64 的 PKCS8（Key Management API 给的格式）也能用
    der = base64.b64encode(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption())).decode()
    h2 = signing.sign_headers("POST", "https://api.ebay.com/post-order/v2/cancellation/1/approve", b"", private_key=der, jwe="J")
    assert "Content-Digest" not in h2 and h2["Signature-Input"].startswith('sig1=("x-ebay-signature-key" "@method"')
    # 没配置 → 不签
    assert signing.sign_headers("POST", "https://x/y", b"{}", private_key="", jwe="") == {}
    assert signing.signature_error('{"errors":[{"errorId":215120}]}') and signing.signature_error("other") is None


def test_issue_refund_carries_signature_when_configured(env, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from app import config

    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                              serialization.NoEncryption()).decode()
    monkeypatch.setenv("EBAY_SIGNING_PRIVATE_KEY", pem)
    monkeypatch.setenv("EBAY_SIGNING_JWE", "JWE-X")
    config.get_settings.cache_clear()
    rid = _submit(env["cs"]).json()["id"]
    r = env["admin"].post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert r.json()["status"] == "REFUNDING"
    req = env["fake"]["ebay"].requests[-1]
    assert req.headers["x-ebay-signature-key"] == "JWE-X" and req.headers["Signature"].startswith("sig1=:")
    from app.ebay.signing import content_digest

    assert req.headers["Content-Digest"] == content_digest(req.content)  # 签的就是实际发出的字节


def test_cancel_full_detail_and_actions(env, monkeypatch):
    """纠纷处理页（/full）：CANCEL 分支给出实时详情、能做的处理、为什么不能做；客服看不到“同意”。"""
    from app import case_actions_common as cac

    detail = {"cancelDetail": {"cancelId": "5460000001", "cancelState": "CANCEL_REQUESTED", "cancelStatus": "CANCEL_PENDING",
                               "cancelReason": "ORDER_MISTAKE", "requestorType": "BUYER",
                               "requestRefundAmount": {"value": 40.55, "currency": "EUR"},
                               "cancelRequestDate": {"value": "2026-11-10T08:00:00.000Z"},
                               "activityHistories": [{"activityType": "BUYER_CREATE_CANCEL", "actionBy": "BUYER",
                                                      "creationDate": {"value": "2026-11-10T08:00:00.000Z"}}]}}
    monkeypatch.setattr(cac, "get_detail", lambda store_id, path, api=None, params=None: detail)
    cid = env["ids"]["cancel"]
    f = env["admin"].get(f"/internal/ebay-cases/{cid}/full").json()
    assert f["live"]["requestor"] == "BUYER" and f["live"]["refund_amount"] == {"value": 40.55, "currency": "EUR"}
    assert f["live"]["history"][0]["actor"] == "买家"
    assert f["actions"]["labels"] == {"approve": "同意取消", "reject": "拒绝取消"}
    assert f["actions"]["can"] == {"approve": True, "reject": True} and f["actions"]["blocked"] is None
    assert f["conversation_id"] == env["ids"]["conv"] and len(f["seller_reasons"]) == 3
    c = env["cs"].get(f"/internal/ebay-cases/{cid}/full").json()
    assert c["actions"]["can"] == {"approve": False, "reject": True} and c["actions"]["senior"]["approve"] is True
    # eBay 上已经关闭：都不能做，并说明原因
    detail["cancelDetail"]["cancelState"] = "CLOSED"
    f = env["admin"].get(f"/internal/ebay-cases/{cid}/full").json()
    assert f["actions"]["can"] == {"approve": False, "reject": False} and f["actions"]["why"]["approve"]
    # 别的店铺（没对接）：整页说明到 eBay 后台
    f = env["admin"].get(f"/internal/ebay-cases/{env['ids']['cancel_x']}/full").json()
    assert f["actions"]["blocked"] and "eBay 后台" in f["actions"]["blocked"]


def test_signature_error_audit_usage_and_overdue(env):
    from datetime import timedelta

    from sqlalchemy import select

    from app import ebay_cases as ec
    from app.db import session_scope
    from app.models import AuditEvent, LlmUsage, RefundRequest

    admin, rq = env["admin"], env["rq"]
    rid = _submit(env["cs"]).json()["id"]
    env["fake"]["ebay"] = FakeEbay((403, {"errors": [{"errorId": 215120, "message": "Signature validation failed"}]}))
    admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    with session_scope() as db:
        a = db.scalar(select(AuditEvent).where(AuditEvent.action == "EBAY_SIGNATURE_ERROR"))
        assert a.store_id == "RS" and a.detail == {"endpoint": "issueRefund", "error_code": "215120"}
        f = db.scalar(select(AuditEvent).where(AuditEvent.action == "REFUND_ISSUE_FAILED"))
        assert f.detail["signature_error_code"] == "215120"
        u = db.scalar(select(LlmUsage).where(LlmUsage.purpose == "ebay_signature"))
        assert (u.ok, u.store_id, u.error) == (False, "RS", "215120 issueRefund")
    # 取消交易的签名错误也记（接口回滚了也留得下）
    env["fake"]["ebay"] = FakeEbay((403, {"error": [{"message": "215001 Missing x-ebay-signature-key header"}]}))
    r = env["as"].post(f"/internal/ebay-cases/{env['ids']['cancel']}/cancel/approve", json={"confirm_order_id": "11-111-003"})
    assert r.status_code == 422
    with session_scope() as db:
        codes = [x.detail["endpoint"] for x in db.scalars(select(AuditEvent).where(AuditEvent.action == "EBAY_SIGNATURE_ERROR"))]
        assert "cancellation.approve" in codes

    # 退款中：流水 references 里带退款 ID 的优先对上；超过 3 天对不上的记“处理异常”
    admin.post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "not_refunded"})
    env["fake"]["ebay"] = FakeEbay((200, {"refundId": "R-77", "refundStatus": "PENDING"}))
    admin.post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    with session_scope() as db:
        ec.save(db, "RS", "REFUND", [
            {"external_id": "RRP-A", "order_id": "11-111-001", "status": "PAYOUT", "is_open": False,
             "opened_at": _now() + timedelta(minutes=1), "amount": 1, "currency": "USD", "detail": {}},
            {"external_id": "RRP-B", "order_id": "11-111-001", "status": "PAYOUT", "is_open": False,
             "opened_at": _now() + timedelta(minutes=2), "amount": 37, "currency": "USD",
             "detail": {"references": [{"type": "REFUND_ID", "id": "R-77"}]}}])
        assert rq.reconcile(db, "RS") == 1
        rr = db.get(RefundRequest, rid)
        from app.models import EbayCase

        assert rr.status == "REFUNDED" and db.get(EbayCase, rr.refund_case_id).external_id == "RRP-B"
        # 再造一张退款中、执行于 4 天前、对不上流水的
        rr.status, rr.refund_case_id, rr.order_id = "REFUNDING", None, "11-111-009"
        rr.executed_at = _now() - timedelta(days=4)
        assert rq.reconcile(db, "RS") == 0
        assert rr.status == "ERROR" and "超过 3 天" in rr.error


def test_return_execution_errors(env, monkeypatch):
    import app.return_actions as ra
    from app.case_actions_common import ActionError

    admin, ids = env["admin"], env["ids"]
    outcome = {"v": ActionError(502, "请求 eBay 出错（ReadTimeout），不确定是否已处理，请到 eBay 后台核对")}

    def fake_exec(db, rr, user, http=None):
        v = outcome["v"]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(ra, "execute_refund_request", fake_exec, raising=False)
    rid = _submit(env["cs"], order_id="11-111-002", case_id=ids["ret"], amount=20.0, confirm_amount=20.0,
                  return_payload={"mode": "full"}).json()["id"]
    ok = {"confirm_amount": 20.0, "confirm_currency": "EUR"}
    # 409（eBay 现在不允许退）：没执行，申请单保持待审核
    outcome["v"] = ActionError(409, "eBay 现在不允许退款")
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json=ok)
    assert r.status_code == 409 and admin.get(f"/internal/refund-requests/{rid}").json()["status"] == "PENDING"
    # 502（不确定）：处理异常
    outcome["v"] = ActionError(502, "请求 eBay 出错（ReadTimeout），不确定是否已处理，请到 eBay 后台核对")
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json=ok)
    assert r.json()["status"] == "ERROR" and "不确定" in r.json()["error"]
    admin.post(f"/internal/refund-requests/{rid}/resolve", json={"outcome": "not_refunded"})
    # 提议部分退款：退款中，注明等买家同意
    outcome["v"] = {"state": "offered", "refund_id": None, "summary": "已向买家提出部分退款 EUR 20.00"}
    r = admin.post(f"/internal/refund-requests/{rid}/approve", json=ok)
    assert r.json()["status"] == "REFUNDING" and "等买家" in r.json()["error"]


def test_rule_audit_and_cancel_failure_audit(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import AuditEvent

    admin, af = env["admin"], env["as"]
    admin.put("/internal/refund-review-rules", json={"rules": [{"store_id": "RS", "currency": "EUR", "min_amount": 5,
                                                                 "max_amount": 50, "mode": "direct", "note": "小额"}]})
    r = _submit(af, full=False, amount=10.0, confirm_amount=10.0)
    assert r.json()["status"] == "REFUNDING"
    with session_scope() as db:
        d = db.scalar(select(AuditEvent).where(AuditEvent.action == "REFUND_DIRECT_BY_RULE")).detail
        assert d["rule"]["store_id"] == "RS" and (d["rule"]["min_amount"], d["rule"]["max_amount"], d["rule"]["currency"]) == (5, 50, "EUR")
        c = db.scalar(select(AuditEvent).where(AuditEvent.action == "REFUND_REQUEST_CREATED")).detail
        assert c["mode"] == "direct" and c["rule"]["id"] == d["rule"]["id"]
    # 取消：eBay 超时 → 接口回滚，但“已尝试、结果不确定”的审计留着
    env["fake"]["ebay"] = FakeEbay(httpx.ReadTimeout("t"))
    r = af.post(f"/internal/ebay-cases/{env['ids']['cancel']}/cancel/approve", json={"confirm_order_id": "11-111-003"})
    assert r.status_code == 502
    with session_scope() as db:
        a = db.scalar(select(AuditEvent).where(AuditEvent.action == "CANCEL_APPROVE_FAILED"))
        assert a.store_id == "RS" and a.detail["uncertain"] is True and a.detail["case_id"] == env["ids"]["cancel"]
        assert "不确定" in a.detail["error"]


def test_local_order_marked_refunded(env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EbayOrder

    def order(oid):
        with session_scope() as db:
            o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == "RS", EbayOrder.order_id == oid))
            return o.refunded, o.cancelled

    rid = _submit(env["cs"]).json()["id"]
    env["admin"].post(f"/internal/refund-requests/{rid}/approve", json={"confirm_amount": 40.55, "confirm_currency": "EUR"})
    assert order("11-111-001") == (True, False)  # 退款后售后原因表马上能看到
    env["fake"]["ebay"] = FakeEbay((200, None))
    env["as"].post(f"/internal/ebay-cases/{env['ids']['cancel']}/cancel/approve", json={"confirm_order_id": "11-111-003"})
    assert order("11-111-003") == (True, True)  # 同意取消：eBay 自动退款


def test_return_refund_blocked_by_open_cancel_only(env):
    """通过退货退款（产品经理 11-15 定）：同一订单进行中的取消申请要拦（409，先处理取消）；别的退货 / 纠纷只提示不拦。"""
    from app.db import session_scope
    from app.models import EbayCase

    cs, ids = env["cs"], env["ids"]
    t = datetime(2026, 11, 11, 8, 0)
    with session_scope() as db:
        other = EbayCase(store_id="RS", kind="CASE", external_id="5200000077", order_id="11-111-002", buyer="buyer_r",
                         status="OPEN", is_open=True, opened_at=t)
        db.add(other)
    pf = cs.get("/internal/refund-requests/prefill", params={"case_id": ids["ret"]}).json()
    assert pf["blocked"] is None and pf["can_submit"]
    assert "纠纷 5200000077" in pf["open_case_notice"] and "重复退款" in pf["open_case_notice"]
    with session_scope() as db:
        db.add(EbayCase(store_id="RS", kind="CANCEL", external_id="5460000055", order_id="11-111-002", buyer="buyer_r",
                        status="CANCEL_REQUESTED", is_open=True, opened_at=t))
    pf = cs.get("/internal/refund-requests/prefill", params={"case_id": ids["ret"]}).json()
    assert "取消申请（5460000055）" in pf["blocked"] and not pf["can_submit"]
    r = _submit(cs, order_id="11-111-002", case_id=ids["ret"], amount=20.0, confirm_amount=20.0,
                return_payload={"mode": "full"})
    assert r.status_code == 409 and "先到“取消交易”" in r.json()["detail"]
