"""第七轮验收（售后写操作 / 退款审核）的测试脚手架：假 eBay、店铺与角色、种子数据、接口对照表、断言辅助。

用法见 tests/test_round7_acceptance.py。两位开发交付后，只需要核对本文件里的 TARGETS（路由、请求体字段）和
REFUND_ROUTES，用例不用改。

假 eBay 在 httpx 底层拦截：发往 api.ebay.com / apiz.ebay.com 的请求交给 FakeEbay（记下来、按设定返回），
其他非本机连接一律报错——保证不会真调 eBay 或任何外部系统。开发代码自己传了 MockTransport 的不受影响。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import login, make_user

EBAY_HOSTS = ("api.ebay.com", "apiz.ebay.com", "api.sandbox.ebay.com", "apiz.sandbox.ebay.com")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")

LIVE, OTHER = "R7A", "R7B"  # 对接店铺 / 非对接店铺
LIVE_NAME = "验收店铺A"
ORDER_TOTAL, CURRENCY = 50.00, "EUR"
BUYER = "r7buyer"
ORDER2 = "11-11111-22222"  # 对接店铺同一买家的第二张订单：没有任何售后记录（退款申请、主动取消用）
ITEM = "111000000001"


# ---------------------------------------------------------------- 假 eBay
@dataclass
class Call:
    method: str
    url: str
    path: str
    body: object
    headers: dict


@dataclass
class FakeEbay:
    """按 (方法, 路径正则) 设定返回；默认 GET 返回 GET_DEFAULTS 里匹配的详情，写请求返回 200 {}。"""
    calls: list[Call] = field(default_factory=list)
    rules: list[tuple] = field(default_factory=list)

    def on(self, method: str, pattern: str, status: int = 200, body: object | None = None, exc: Exception | None = None):
        self.rules.insert(0, (method.upper(), re.compile(pattern), status, body, exc))
        return self

    def fail_writes(self, status: int | None = None, exc: Exception | None = None):
        """所有写请求超时（exc）或返回 status。"""
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.on(m, ".*", status or 200, {"errors": [{"message": "server error"}]} if status else None, exc)
        return self

    @property
    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = request.content[:200]
        call = Call(request.method, str(request.url), request.url.path, body, dict(request.headers))
        self.calls.append(call)
        for method, rx, status, resp, exc in self.rules:
            if method == request.method and rx.search(request.url.path):
                if exc:
                    raise exc
                return httpx.Response(status, json=resp if resp is not None else {})
        m = re.search(r"/sell/fulfillment/v1/order/([^/]+)$", request.url.path)
        if request.method == "GET" and m:
            return httpx.Response(200, json={**LIVE_ORDER, "orderId": m.group(1)})
        for rx, resp in (GET_DEFAULTS if request.method == "GET" else WRITE_DEFAULTS):
            if re.search(rx, request.url.path):
                return httpx.Response(200, json=resp)
        return httpx.Response(200, json={})


# GET 默认返回（eBay 实时详情）：结构照 1 号 / 2 号用例里的样例，金额对齐种子数据（50 EUR）
RETURN_ID, CASE_ID, CANCEL_ID = "R7RET1R7A", "R7CASE1R7A", "R7CAN1R7A"
RETURN_OPTIONS = ["SELLER_APPROVE_REQUEST", "SELLER_PROVIDE_LABEL", "SELLER_ISSUE_REFUND", "SELLER_MARK_AS_RECEIVED",
                  "SELLER_SEND_MESSAGE"]
RETURN_DETAIL = {
    "summary": {"returnId": RETURN_ID, "orderId": "11-11111-11111", "buyerLoginName": BUYER, "state": "RETURN_REQUESTED",
                "status": "WAITING_FOR_RETURN_LABEL", "currentType": "MONEY_BACK",
                "creationInfo": {"reason": "DEFECTIVE_ITEM", "comments": {"content": "broken"},
                                 "creationDate": {"value": "2026-11-10T08:00:00.000Z"}},
                "sellerResponseDue": {"activityDue": "SELLER_APPROVE_REQUEST", "respondByDate": {"value": "2026-11-15T08:00:00.000Z"}},
                "sellerAvailableOptions": [{"actionType": o} for o in RETURN_OPTIONS], "returnPolicy": {"rmaRequired": False}},
    "detail": {"refundInfo": {"estimatedRefundDetail": {"itemizedRefundDetails": [
        {"refundFeeType": "PURCHASE_PRICE", "estimatedAmount": {"value": 45.0, "currency": CURRENCY},
         "minAmount": {"value": 10.0, "currency": CURRENCY}, "maxAmount": {"value": 45.0, "currency": CURRENCY}},
        {"refundFeeType": "ORIGINAL_SHIPPING", "estimatedAmount": {"value": 5.0, "currency": CURRENCY},
         "minAmount": {"value": 5.0, "currency": CURRENCY}, "maxAmount": {"value": 5.0, "currency": CURRENCY}}]}},
        "responseHistory": [], "returnShipmentInfo": {}},
}
CASE_DETAIL = {"caseId": CASE_ID, "status": "OPEN", "caseType": "RETURN", "returnId": RETURN_ID,
               "claimAmount": {"value": ORDER_TOTAL, "currency": CURRENCY}, "appealDetails": {"eligibleForAppeal": True},
               "caseDetails": {"creationDate": {"value": "2026-11-09T00:00:00.000Z"}}, "caseHistoryDetails": {"history": []}}
CANCEL_DETAIL = {"cancelDetail": {"cancelId": CANCEL_ID, "cancelState": "REQUESTED", "cancelStatus": "CANCEL_REQUESTED",
                                  "requestorType": "BUYER", "cancelReason": "BUYER_ASKED_CANCEL", "legacyOrderId": "11-11111-11111",
                                  "requestRefundAmount": {"value": ORDER_TOTAL, "currency": CURRENCY}}}
LIVE_ORDER = {"orderId": "11-11111-11111", "orderPaymentStatus": "PAID",
              "pricingSummary": {"total": {"value": "50.00", "currency": CURRENCY}}, "paymentSummary": {"refunds": []},
              "lineItems": [{"lineItemId": "L1", "sku": "SKU-R7", "lineItemCost": {"value": "50.00", "currency": CURRENCY}}]}
GET_DEFAULTS = [
    (r"/post-order/v2/return/", RETURN_DETAIL),
    (r"/post-order/v2/casemanagement/", CASE_DETAIL),
    (r"/post-order/v2/cancellation/", CANCEL_DETAIL),
    (r"/post-order/v2/inquiry/", {"inquiryStatusEnum": "OPEN", "claimAmount": {"value": ORDER_TOTAL, "currency": CURRENCY}}),
    (r"/sell/fulfillment/v1/order/", LIVE_ORDER),
]
# 写请求的默认返回（退款要有 refundId；可取消检查要 eligible；上传文件要 fileId）
WRITE_DEFAULTS = [
    (r"/issue_refund", {"refundId": "R7-REFUND-1", "refundStatus": "PENDING"}),
    (r"/cancellation/check_eligibility", {"eligible": True}),
    (r"/file/upload", {"fileId": "R7-FILE-1"}),
]


@pytest.fixture()
def ebay(monkeypatch):
    """装好假 eBay 并返回它；同时禁止任何非本机、非 eBay 的连接。"""
    fake = FakeEbay()
    real = httpx.HTTPTransport.handle_request

    def handle(self, request):
        host = request.url.host or ""
        if host in EBAY_HOSTS:
            return fake.handle(request)
        if host in LOCAL_HOSTS:
            return real(self, request)
        raise httpx.ConnectError(f"验收测试禁止连外网：{host}", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    monkeypatch.setattr("app.ebay.auth.EbayTokenManager.access_token", lambda self, *a, **k: "AT")
    return fake


# ---------------------------------------------------------------- 店铺、角色、种子数据
@dataclass
class World:
    cases: dict  # (店铺, 类型) → EbayCase.id
    conv: int  # 对接店铺买家的会话 ID
    order_id: str
    admin: TestClient
    aftersales: TestClient
    cs: TestClient


@pytest.fixture()
def world(client, ebay, monkeypatch):
    """对接店铺 R7A（EBAY_CHANNEL=ebay、在 EBAY_LIVE_STORES 里）和非对接店铺 R7B，各有一条退货 / 纠纷 / 取消 / 咨询；
    对接店铺买家有订单（50 EUR）和会话；管理员 / 售后 / 客服三个已登录的客户端。"""
    from app import config
    from app.db import session_scope
    from app.models import Conversation, EbayCase, EbayOrder, EbayOrderLine, Store

    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", LIVE_NAME)
    monkeypatch.setenv("SEND_DELAY_SECONDS", "300")  # 和正式环境一样：发件箱 5 分钟后才发，之前可撤回
    config.get_settings.cache_clear()
    now = datetime.utcnow()
    cases = {}
    with session_scope() as db:
        db.add(Store(id=LIVE, name=LIVE_NAME, default_marketplace_id="EBAY_DE"))
        db.add(Store(id=OTHER, name="验收店铺B", default_marketplace_id="EBAY_DE"))
        db.flush()
        for store in (LIVE, OTHER):
            oid = "11-11111-11111" if store == LIVE else "22-22222-22222"
            db.add(EbayOrder(store_id=store, order_id=oid, buyer=BUYER, item_id=ITEM, item_ids=ITEM, skus="SKU-R7",
                             created_at=now - timedelta(days=10), total=ORDER_TOTAL, currency=CURRENCY))
            db.add(EbayOrderLine(store_id=store, order_id=oid, line_item_id=f"L-{store}", line_key=f"{ITEM}-{store}9",
                                 item_id=ITEM))
            if store == LIVE:
                db.add(EbayOrder(store_id=store, order_id=ORDER2, buyer=BUYER, item_id=ITEM, item_ids=ITEM, skus="SKU-R7",
                                 created_at=now - timedelta(days=5), total=ORDER_TOTAL, currency=CURRENCY))
            for kind, ext, ref, reason in (("RETURN", "R7RET1", oid, "DEFECTIVE_ITEM"), ("CASE", "R7CASE1", f"{ITEM}-{store}9", "ITEM_NOT_RECEIVED"),
                                           ("CANCEL", "R7CAN1", oid, "BUYER_ASKED_CANCEL"), ("INQUIRY", "R7INQ1", f"{ITEM}-{store}9", None)):
                c = EbayCase(store_id=store, kind=kind, external_id=f"{ext}{store}", order_id=ref, buyer=BUYER, status="OPEN",
                             is_open=True, amount=ORDER_TOTAL, currency=CURRENCY, reason=reason, opened_at=now - timedelta(days=2),
                             item_id=ITEM)
                db.add(c)
                db.flush()
                cases[(store, kind)] = c.id
        conv = Conversation(store_id=LIVE, ebay_conversation_id="R7-CONV", buyer_key=BUYER, item_id=ITEM,
                            conversation_type="FROM_MEMBERS", marketplace_id="EBAY_DE", channel_source="ebay")
        db.add(conv)
        db.flush()
        conv_id = conv.id
    for name, role in (("r7_aftersales", "AFTERSALES"), ("r7_cs", "CS")):
        make_user(name, role, stores=[LIVE, OTHER])
    clients = {}
    for name in ("r7_aftersales", "r7_cs"):
        c = TestClient(client.app)
        login(c, name)
        clients[name] = c
    yield World(cases, conv_id, "11-11111-11111", client, clients["r7_aftersales"], clients["r7_cs"])
    config.get_settings.cache_clear()


# ---------------------------------------------------------------- 接口对照表（交付后核对这里）
@dataclass
class Action:
    payload: dict
    senior: bool = False  # 只有管理员 / 售后能做（退款、接受退货、同意取消）
    message: bool = False  # 给买家发信：必须走发件箱
    money: bool = False  # 涉及金额：要核对
    route_action: str | None = None  # 路由里的动作名（同一动作不同方式时用，如 accept 的四种方式）


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # 1x1 图片

TARGETS: dict[str, dict] = {
    # 现有：咨询（inquiry_actions，2026-11-12 已重构到 case_actions_common）
    "inquiry": {"kind": "INQUIRY", "route": "/internal/ebay-cases/{id}/inquiry/{action}", "actions": {
        "message": Action({"text": "Hello, we are checking."}, message=True),
        "shipment": Action({"tracking_number": "GN000000001GB", "carrier": "Royal Mail"}),
        "refund": Action({"amount": ORDER_TOTAL, "currency": CURRENCY, "confirm": True}, senior=True, money=True),
    }},
    # 1 号：退货（return_actions：accept / refund / received / file / message）
    "return": {"kind": "RETURN", "route": "/internal/ebay-cases/{id}/return/{action}", "actions": {
        "accept_upload_label": Action({"method": "upload_label", "tracking_number": "1Z000000000000001", "carrier": "DHL",
                                       "file_data": PNG, "file_name": "label.png"}, senior=True, route_action="accept"),
        "accept_no_label": Action({"method": "no_label", "no_label_reason": "ITEM_TOO_LARGE", "comment": "too large"},
                                  senior=True, route_action="accept"),
        "accept_ebay_label": Action({"method": "ebay_label", "carrier_enum": "DHL"}, senior=True, route_action="accept"),
        "accept_only": Action({"method": "accept_only"}, senior=True, route_action="accept"),
        "received": Action({}, senior=True),
        "message": Action({"text": "Hello, please send the item back."}, message=True),
        "file": Action({"file_data": PNG, "file_name": "photo.png", "purpose": "ITEM_RELATED", "comment": "photo"}),
    }},
    # 1 号：纠纷（case_actions：appeal / refund / message / file；退货升级来的转到那条退货）
    "case": {"kind": "CASE", "route": "/internal/ebay-cases/{id}/case/{action}", "actions": {
        "appeal": Action({"comment": "We shipped on time, see tracking."}, senior=True),
        "message": Action({"text": "Hello, we have shipped it."}, message=True),
        "file": Action({"file_data": PNG, "file_name": "pod.png", "purpose": "LABEL_RELATED", "comment": "proof of delivery"}),
    }},
    # 2 号：取消交易（cancel_actions：approve / reject）
    "cancel": {"kind": "CANCEL", "route": "/internal/ebay-cases/{id}/cancel/{action}", "actions": {
        "approve": Action({"confirm_order_id": "11-11111-11111"}, senior=True),
        "reject": Action({"reason": "已发货", "shipped_at": "2026-11-10", "tracking_number": "1Z000000000000002"}),
    }},
}

# 2 号：退款申请单 + 审核规则（api/refund_requests.py）
REFUND_ROUTES = {
    "create": ("POST", "/internal/refund-requests"),
    "approve": ("POST", "/internal/refund-requests/{id}/approve"),
    "reject": ("POST", "/internal/refund-requests/{id}/reject"),
    "list": ("GET", "/internal/refund-requests"),
    "rules": ("GET", "/internal/refund-review-rules"),
    "rules_save": ("PUT", "/internal/refund-review-rules"),
    "seller_cancel": ("POST", "/internal/order-cancellations"),
}


def refund_request_body(world: "World", amount: float = 20.0, source: str = "MANUAL", **kw) -> dict:
    return {"store_id": LIVE, "order_id": ORDER2, "conversation_id": world.conv, "amount": amount, "currency": CURRENCY,
            "reason": "ITEM_NOT_AS_DESCRIBED", "source": source, "full": False, "confirm_amount": amount,
            "confirm_currency": CURRENCY, **kw}


def return_refund_body(world: "World", kind: str = "RETURN", mode: str = "full", amount: float = ORDER_TOTAL, **kw) -> dict:
    """退货 / 纠纷退款：提交退款申请单，执行方式 post_order_return（审核通过或命中直接退款规则后，通过那条退货退）。"""
    return {"store_id": LIVE, "order_id": world.order_id, "conversation_id": world.conv, "case_id": world.cases[(LIVE, kind)],
            "source": "AFTERSALES", "execution": "post_order_return", "return_payload": {"mode": mode, "keep_item": False},
            "full": mode == "full", "amount": amount, "currency": CURRENCY, "reason": "ITEM_NOT_AS_DESCRIBED",
            "confirm_amount": amount, "confirm_currency": CURRENCY, **kw}


def refund_rules_body(store: str | None = LIVE, lo: float = 0, hi: float = 30, mode: str = "direct") -> dict:
    return {"rules": [{"store_id": store, "min_amount": lo, "max_amount": hi, "currency": CURRENCY, "mode": mode}]}


# ---------------------------------------------------------------- 路由是否已交付
def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _routes(app) -> list[tuple[str, set]]:
    """全部 (路径, 方法)：用 OpenAPI 文档取（不依赖 FastAPI 内部的路由嵌套结构）。"""
    out = []
    for p, ops in (app.openapi().get("paths") or {}).items():
        out.append((p, {m.upper() for m in ops}))
    return out


def route_exists(app, method: str, path: str) -> bool:
    """path 用 {id}、{action} 占位或已填好具体动作；和 app 的路由比对（参数名不同也算）。"""
    want = _norm(path)
    for p, methods in _routes(app):
        if method.upper() not in methods:
            continue
        have = _norm(p)
        if have == want:
            return True
        # 路由里动作是参数、我们给的是具体动作（或反过来）
        if re.fullmatch(re.escape(have).replace(r"\{\}", r"[^/]+"), want) or \
                re.fullmatch(re.escape(want).replace(r"\{\}", r"[^/]+"), have):
            return True
    return False


def need(app, method: str, path: str, who: str):
    if not route_exists(app, method, path):
        pytest.skip(f"待{who}交付：没有接口 {method} {path}（交付后核对 round7_support 里的路由）")


def target_path(target: str, action: str, case_id: int) -> str:
    return TARGETS[target]["route"].format(id=case_id, action=action)


def do(world: World, who: str, target: str, action: str, store: str = LIVE, **override):
    """以某个角色（admin / aftersales / cs）对某条记录做一种处理。缺接口时跳过。"""
    t = TARGETS[target]
    spec = t["actions"][action]
    name = spec.route_action or action
    cid = world.cases[(store, t["kind"])]
    client = getattr(world, who)
    need(client.app, "POST", t["route"].format(id="{}", action=name), "1 号" if target in ("return", "case") else
         "2 号" if target == "cancel" else "（现有）")
    return client.post(target_path(target, name, cid), json={**spec.payload, **override})


def all_actions(*targets: str, flag: str | None = None):
    """参数化用：[(目标, 动作)]；flag = senior / message / money 只取这类。"""
    out = []
    for t in targets or TARGETS:
        for a, spec in TARGETS[t]["actions"].items():
            if flag is None or getattr(spec, flag):
                out.append(pytest.param(t, a, id=f"{t}-{a}"))
    return out


# ---------------------------------------------------------------- 读库断言
def audits(store: str | None = None, since_id: int = 0) -> list:
    from app.db import session_scope
    from app.models import AuditEvent

    with session_scope() as db:
        q = select(AuditEvent).where(AuditEvent.id > since_id)
        if store:
            q = q.where(AuditEvent.store_id == store)
        return [(a.action, a.actor, a.detail) for a in db.scalars(q.order_by(AuditEvent.id))]


def last_audit_id() -> int:
    from app.db import session_scope
    from app.models import AuditEvent

    with session_scope() as db:
        return db.scalar(select(func.max(AuditEvent.id))) or 0


def case_state(case_id: int) -> tuple:
    from app.db import session_scope
    from app.models import EbayCase, EbayCaseNote

    with session_scope() as db:
        c = db.get(EbayCase, case_id)
        notes = db.scalar(select(func.count()).select_from(EbayCaseNote).where(EbayCaseNote.case_id == case_id))
        return c.status, c.is_open, c.work_state, notes


def outbox(conv_id: int) -> list:
    from app.db import session_scope
    from app.models import OutboundMessage

    with session_scope() as db:
        return [(o.state, o.source, o.send_after, o.body) for o in
                db.scalars(select(OutboundMessage).where(OutboundMessage.conversation_id == conv_id).order_by(OutboundMessage.id))]


def conv_events(conv_id: int) -> list:
    from app.db import session_scope
    from app.models import ConversationEvent

    with session_scope() as db:
        return [(e.event_type, e.detail) for e in
                db.scalars(select(ConversationEvent).where(ConversationEvent.conversation_id == conv_id).order_by(ConversationEvent.id))]


def conv_aftersales(conv_id: int) -> tuple:
    from app.db import session_scope
    from app.models import Conversation

    with session_scope() as db:
        c = db.get(Conversation, conv_id)
        return c.aftersales_status, c.aftersales_note


# ---------------------------------------------------------------- 记下送给模型的提示词（AI 下一次起草能不能看到事件）
def capture_prompts(outputs: list[str]) -> list:
    """装一个假模型：按顺序返回 outputs，并把每次收到的消息文本记进返回的列表。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.config import get_settings
    from app.providers.chat import ChatModelProvider, set_chat_provider_override

    seen: list[str] = []

    class Capturing(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append("\n".join(str(m.content) for m in messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    set_chat_provider_override(ChatModelProvider(get_settings(), model=Capturing(messages=iter([AIMessage(content=o) for o in outputs]))))
    return seen
