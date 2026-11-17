"""第七轮验收：售后写操作（退货 / 纠纷 / 取消）与退款审核。用例清单见 测试输出/第七轮验收用例_售后写操作与退款审核_20261112.md。

全部用假 eBay（round7_support.ebay：httpx 底层拦截，禁止外网），不调模型、不碰外部系统。
接口没交付的自动跳过（“待 1 号 / 2 号交付”）；现有“咨询”作为对照目标，已知问题标 strict xfail（P0-1 ~ P0-4）。"""
from datetime import datetime, timedelta

import httpx
import pytest

from round7_support import (LIVE, ORDER2, ORDER_TOTAL, OTHER, REFUND_ROUTES, TARGETS, all_actions, audits, capture_prompts,  # noqa: F401
                            case_state, conv_aftersales, conv_events, do, ebay, last_audit_id, need, outbox,
                            refund_request_body, refund_rules_body, return_refund_body, world)

pytestmark = pytest.mark.acceptance_slow  # 约 9 分钟：默认全量不跑；正式验收 / CI 用 -m acceptance_slow（见 conftest）

KNOWN: dict = {  # 已知不满足的验收点（strict：满足了会提示去掉）。2026-11-12 全部已修、已清空：
    # P0-1 发信走发件箱、P0-2 会话事件 / 售后状态、P0-3 起草能看到事件（1 号重构咨询处理）；
    # P0-4 咨询失败审计、P0-2b 咨询退款后售后原因表、D2 取消失败审计（2 号改成 cac.record_failure）；
    # D1 / D2 / D3（退货、纠纷）：退货 / 纠纷退款改走退款申请单，用 7b 节的用例复测
}


def check(ok: bool, target: str, point: str, msg: str):
    """先检查再判断：已知问题（KNOWN）确实不满足 → xfail；已知问题已经满足 → 失败提示去掉 KNOWN；其余按 ok 断言。"""
    if (target, point) in KNOWN:
        if ok:
            pytest.fail(f"{KNOWN[(target, point)]} —— 已经满足了，请从 KNOWN 里去掉")
        pytest.xfail(KNOWN[(target, point)])
    assert ok, msg


def _detail(r) -> str:
    try:
        d = r.json().get("detail")
    except ValueError:
        return r.text
    return d if isinstance(d, str) else str(d)


WRITE_ACTIONS = [p for p in all_actions() if not TARGETS[p.values[0]]["actions"][p.values[1]].message]


# ================================================================ 1. 店铺范围
@pytest.mark.parametrize("target, action", all_actions())
def test_other_store_refused(world, ebay, target, action):
    r = do(world, "admin", target, action, store=OTHER)
    assert r.status_code == 403, r.text
    assert "eBay 后台" in _detail(r)
    assert ebay.writes == [], [c.path for c in ebay.writes]


@pytest.mark.parametrize("target, action", all_actions())
def test_mock_channel_refused(world, ebay, monkeypatch, target, action):
    from app import config

    monkeypatch.setenv("EBAY_CHANNEL", "mock")
    config.get_settings.cache_clear()
    r = do(world, "admin", target, action)
    assert r.status_code == 403 and "eBay" in _detail(r), r.text
    assert ebay.writes == []


# ================================================================ 2. 角色
@pytest.mark.parametrize("target, action", all_actions(flag="senior"))
def test_senior_actions_cs_forbidden(world, ebay, target, action):
    before = last_audit_id()
    r = do(world, "cs", target, action)
    assert r.status_code == 403, r.text
    assert ebay.writes == []
    assert not [a for a in audits(LIVE, before) if "FAIL" not in a[0] and "DENIED" not in a[0]], "不应记“已处理”审计"


@pytest.mark.parametrize("target, action", all_actions(flag="senior"))
def test_senior_actions_aftersales_allowed(world, ebay, target, action):
    r = do(world, "aftersales", target, action)
    assert r.status_code < 400, r.text


# ================================================================ 3. 金额核对
@pytest.mark.parametrize("target, action", all_actions(flag="money"))
@pytest.mark.parametrize("bad", [{"amount": ORDER_TOTAL + 1}, {"currency": "USD"}, {"confirm": False}, {"amount": 0},
                                 {"amount": -5}, {"amount": "abc"}], ids=["more", "currency", "no_confirm", "zero", "negative", "nan"])
def test_amount_checks(world, ebay, target, action, bad):
    if "partial" in action and "amount" in bad and bad["amount"] == ORDER_TOTAL + 1:
        bad = {"amount": ORDER_TOTAL + 0.01}  # 部分退款超过订单金额
    r = do(world, "admin", target, action, **bad)
    assert r.status_code in (409, 422), (bad, r.status_code, r.text)
    assert ebay.writes == []


@pytest.mark.parametrize("target, action", all_actions(flag="money"))
def test_refund_blocked_when_ebay_says_refunded(world, ebay, target, action):
    ebay.on("GET", r"/sell/fulfillment/v1/order/", 200, {"orderPaymentStatus": "FULLY_REFUNDED",
                                                         "pricingSummary": {"total": {"value": str(ORDER_TOTAL), "currency": "EUR"}},
                                                         "paymentSummary": {"refunds": [{"amount": {"value": str(ORDER_TOTAL)}}]}})
    r = do(world, "admin", target, action)
    check(r.status_code == 409 and not ebay.writes, target, "refunded_check",
          f"eBay 显示已全额退款时不能再退：HTTP {r.status_code}，写请求 {[c.path for c in ebay.writes]}")


# ================================================================ 4. 超时 / 5xx 不重试
@pytest.mark.parametrize("target, action", WRITE_ACTIONS)
@pytest.mark.parametrize("failure", ["timeout", "500", "503"])
def test_timeout_and_5xx_no_retry_state_unchanged(world, ebay, target, action, failure):
    if failure == "timeout":
        ebay.fail_writes(exc=httpx.ReadTimeout("timed out"))
    else:
        ebay.fail_writes(status=int(failure))
    cid = world.cases[(LIVE, TARGETS[target]["kind"])]
    before_state, before_audit = case_state(cid), last_audit_id()
    r = do(world, "admin", target, action)
    assert r.status_code == 502, r.text
    assert "不确定" in _detail(r) and "eBay" in _detail(r)
    assert len(ebay.writes) == 1, [c.path for c in ebay.writes]  # 只发一次（多步的接受退货：第一步失败就停）
    assert case_state(cid)[:3] == before_state[:3]  # eBay 状态、是否开着、处理进度都没变（可以多一条“[结果不确定]”备注）
    failed = [a for a in audits(LIVE, before_audit) if a[0].endswith("_FAILED") or "uncertain" in str(a[2])]
    check(bool(failed), target, "audit_fail", f"失败也要有审计（谁、什么时候、哪条、结果不确定）：{audits(LIVE, before_audit)}")


@pytest.mark.parametrize("target, action", WRITE_ACTIONS)
def test_4xx_reports_ebay_message(world, ebay, target, action):
    for m in ("POST", "PUT"):
        ebay.on(m, ".*", 400, {"errors": [{"message": "Case already closed"}]})
    r = do(world, "admin", target, action)
    assert r.status_code == 422 and "Case already closed" in _detail(r), r.text
    assert len(ebay.writes) == 1


# ================================================================ 5. 给买家发信必须走发件箱
@pytest.mark.parametrize("target, action", all_actions(flag="message"))
def test_message_goes_through_outbox(world, ebay, target, action):
    before = outbox(world.conv)
    r = do(world, "admin", target, action)
    assert r.status_code < 400, r.text
    new = outbox(world.conv)[len(before):]
    ok = not ebay.writes and len(new) == 1 and new[0][0] == "PENDING"         and new[0][2] >= datetime.utcnow() + timedelta(minutes=4)         and new[0][3].strip() == TARGETS[target]["actions"][action].payload["text"]
    check(ok, target, "outbox", f"发信要进发件箱待发（5 分钟可撤回），不能直接调 eBay：eBay 写请求 {len(ebay.writes)}，发件箱新增 {new}")


@pytest.mark.parametrize("target, action", all_actions(flag="message"))
def test_message_compliance_checked(world, ebay, target, action):
    r = do(world, "admin", target, action, text="Please contact me at seller@gmail.com or WhatsApp +49 170 1234567")
    pending = [o for o in outbox(world.conv) if o[0] == "PENDING"]
    ok = not ebay.writes and (r.status_code in (409, 422) or not pending)
    check(ok, target, "outbox", f"带站外联系方式的发信应被合规检查拦下：HTTP {r.status_code}，eBay 写请求 {len(ebay.writes)}")


# ================================================================ 6. 成功后的记录
@pytest.mark.parametrize("target, action", WRITE_ACTIONS)
def test_success_side_effects(world, ebay, target, action):
    cid = world.cases[(LIVE, TARGETS[target]["kind"])]
    before_notes, before_audit, before_events = case_state(cid)[3], last_audit_id(), len(conv_events(world.conv))
    before_as = conv_aftersales(world.conv)
    r = do(world, "admin", target, action)
    assert r.status_code < 400, r.text
    assert ebay.writes, "要调 eBay"  # 接受退货 + 上传面单是三步：上传文件、同意、提供面单
    assert case_state(cid)[3] >= before_notes + 1, "处理备注（EbayCaseNote）要增加"
    new = audits(LIVE, before_audit)
    assert new and any(TARGETS[target]["kind"] in a[0] or target.upper() in a[0] for a in new), new
    ok = len(conv_events(world.conv)) == before_events + 1 and conv_aftersales(world.conv) != before_as
    check(ok, target, "event", "买家会话里要有一条系统事件，会话售后状态 / 备注要更新")


@pytest.mark.parametrize("target, action", [p for p in all_actions(flag="money")])
def test_refund_updates_reason_table(world, ebay, target, action):
    from app import aftersales_reasons as ar
    from app.db import session_scope
    from app.models import OrderAftersalesReason as R

    r = do(world, "admin", target, action)
    assert r.status_code < 400, r.text
    ar.refresh_store(LIVE)
    with session_scope() as db:
        row = db.query(R).filter_by(store_id=LIVE, order_id=world.order_id).one_or_none()
        ok = row is not None and bool(row.refunded)
    check(ok, target, "reason", "退款后售后原因表这张订单要标退款")


@pytest.mark.parametrize("target, action", [p for p in all_actions(flag="money")])
def test_ai_next_draft_sees_action(world, ebay, target, action):
    from app.db import session_scope
    from app.ingestion.service import ingest_buyer_message
    from app.models import Conversation
    from app.worker import worker
    from test_pipeline import draft_json

    r = do(world, "admin", target, action)
    assert r.status_code < 400, r.text
    prompts = capture_prompts([draft_json(claims=[])] * 3)
    with session_scope() as db:
        ingest_buyer_message(db, conv=db.get(Conversation, world.conv), ebay_message_id="R7-BUYER-1", body="Any news on my refund?",
                             sent_at=datetime.utcnow(), skip_merge_window=True, actor="r7")
    worker.run_once()
    assert prompts, "没有起草（检查起草流程）"
    text = "\n".join(prompts)
    events = [d for _, d in conv_events(world.conv) if d]
    want = (events[-1] if events else "")[:20]  # 这次处理写进会话的系统事件说明（取前 20 字）
    check(bool(want) and want in text, target, "ai", f"起草时送给模型的内容里要能看到这次处理（会话事件“{want}”）")


# ================================================================ 7. 退款申请单与审核规则（2 号）
def _refund(world, who, key, rid=None, **body):
    method, path = REFUND_ROUTES[key]
    need(world.admin.app, method, path.replace("{id}", "{}"), "2 号")
    client = getattr(world, who)
    url = path.format(id=rid) if rid is not None else path
    return client.request(method, url, json=body or None)


def _status(resp) -> str:
    d = resp.json()
    return str(d.get("status") or (d.get("item") or {}).get("status") or "")


def _ok_id(resp):
    assert resp.status_code < 400, resp.text
    return resp.json()["id"]


def _issue_refunds(ebay):
    return [c for c in ebay.writes if "issue_refund" in c.path]


def test_no_rule_everything_needs_review(world, ebay):
    r = _refund(world, "aftersales", "create", **refund_request_body(world, 20))
    assert r.status_code < 400, r.text
    assert _status(r) == "PENDING", r.text
    assert ebay.writes == []


def test_rule_in_range_auto_refunds_with_audit(world, ebay):
    assert _refund(world, "admin", "rules_save", **refund_rules_body(LIVE, 0, 30)).status_code < 400
    before = last_audit_id()
    r = _refund(world, "aftersales", "create", **refund_request_body(world, 20))
    assert r.status_code < 400, r.text
    assert len(_issue_refunds(ebay)) == 1, [c.path for c in ebay.writes]
    new = audits(LIVE, before)
    assert any((a[2] or {}).get("mode") == "direct" or "RULE" in a[0].upper() for a in new), \
        f"按规则直接退款也要有审计（方式、金额、操作人）：{new}"
    assert any(a[0] == "REFUND_ISSUED" and a[1] == "r7_aftersales" for a in new), new


def test_rule_hit_by_cs_still_reviewed(world, ebay):
    """客服提交、金额命中直接退款规则：不直接退（只有管理员 / 售后提交才直接退），进待审核。"""
    assert _refund(world, "admin", "rules_save", **refund_rules_body(LIVE, 0, 30)).status_code < 400
    r = _refund(world, "cs", "create", **refund_request_body(world, 20))
    assert r.status_code < 400 and _status(r) == "PENDING", r.text
    assert ebay.writes == []


@pytest.mark.parametrize("amount, store", [(30.01, LIVE), (20, OTHER)], ids=["over_range", "other_store"])
def test_rule_out_of_range_or_other_store_no_auto(world, ebay, amount, store):
    assert _refund(world, "admin", "rules_save", **refund_rules_body(LIVE, 0, 30)).status_code < 400
    body = refund_request_body(world, amount)
    if store == OTHER:
        body = {**body, "store_id": OTHER, "order_id": "22-22222-22222", "conversation_id": None}
    r = _refund(world, "aftersales", "create", **body)
    if store == OTHER:
        assert r.status_code == 403 or _status(r) == "PENDING", r.text  # 非对接店铺不能直接退
    else:
        assert r.status_code < 400 and _status(r) == "PENDING", r.text
    assert ebay.writes == []


def test_rule_boundary_inclusive(world, ebay):
    """规则上限等于金额：按“含”处理（和页面说明一致时通过；不一致请产品定）。"""
    assert _refund(world, "admin", "rules_save", **refund_rules_body(LIVE, 0, 20)).status_code < 400
    r = _refund(world, "aftersales", "create", **refund_request_body(world, 20))
    assert r.status_code < 400, r.text
    assert len(_issue_refunds(ebay)) == 1


def test_approve_roles_and_once(world, ebay):
    rid = _ok_id(_refund(world, "cs", "create", **refund_request_body(world, 20)))
    ok = {"confirm_amount": 20, "confirm_currency": "EUR"}
    assert _refund(world, "cs", "approve", rid, **ok).status_code == 403
    assert ebay.writes == []
    assert _refund(world, "aftersales", "approve", rid, **ok).status_code < 400
    again = _refund(world, "aftersales", "approve", rid, **ok)  # 重复点：不再调 eBay
    assert again.status_code in (409, 422) or again.status_code < 400
    assert len(_issue_refunds(ebay)) == 1


def test_reject_no_ebay_call(world, ebay):
    rid = _ok_id(_refund(world, "cs", "create", **refund_request_body(world, 15)))
    assert _refund(world, "aftersales", "reject", rid, note="不符合退款条件").status_code < 400
    assert ebay.writes == []


def test_approve_amount_tamper(world, ebay):
    rid = _ok_id(_refund(world, "cs", "create", **refund_request_body(world, 20)))
    r = _refund(world, "aftersales", "approve", rid, confirm_amount=45, confirm_currency="EUR")
    assert r.status_code in (409, 422), r.text
    r = _refund(world, "aftersales", "approve", rid, confirm_amount=20, confirm_currency="USD")
    assert r.status_code in (409, 422), r.text
    assert ebay.writes == []


@pytest.mark.parametrize("amount", [ORDER_TOTAL + 0.01, 0, -1], ids=["over_total", "zero", "negative"])
def test_create_amount_limits(world, ebay, amount):
    r = _refund(world, "aftersales", "create", **refund_request_body(world, amount))
    assert r.status_code in (409, 422), r.text
    assert ebay.writes == []


def test_one_open_request_per_order(world, ebay):
    _ok_id(_refund(world, "cs", "create", **refund_request_body(world, 10)))
    r = _refund(world, "cs", "create", **refund_request_body(world, 5))
    assert r.status_code in (409, 422), r.text


def test_approve_timeout_not_resent(world, ebay):
    rid = _ok_id(_refund(world, "cs", "create", **refund_request_body(world, 20)))
    ebay.fail_writes(exc=httpx.ReadTimeout("t"))
    before = last_audit_id()
    r = _refund(world, "aftersales", "approve", rid, confirm_amount=20, confirm_currency="EUR")
    # 两种都可以：直接报 502；或者返回 200、退款单进“处理异常”（ERROR），提示去 eBay 核对
    assert r.status_code == 502 and "eBay" in _detail(r) or r.status_code < 400 and _status(r) == "ERROR", r.text
    _refund(world, "aftersales", "approve", rid, confirm_amount=20, confirm_currency="EUR")  # 结果不确定：不能再点通过重发
    assert len(ebay.writes) == 1
    assert audits(LIVE, before), "超时也要有审计"


def test_rules_admin_only(world, ebay):
    assert _refund(world, "cs", "rules_save", **refund_rules_body()).status_code == 403
    assert _refund(world, "aftersales", "rules_save", **refund_rules_body()).status_code == 403


# ================================================================ 8. 卖家主动取消（会话侧栏，2 号）
def test_seller_cancel(world, ebay):
    body = {"store_id": LIVE, "order_id": ORDER2, "conversation_id": world.conv,
            "reason": "OUT_OF_STOCK_OR_CANNOT_FULFILL", "confirm_order_id": ORDER2}
    assert _refund(world, "cs", "seller_cancel", **body).status_code == 403
    assert not [c for c in ebay.writes if c.path.endswith("/cancellation")]
    r = _refund(world, "aftersales", "seller_cancel", **body)
    assert r.status_code < 400, r.text
    assert len([c for c in ebay.writes if c.path.rstrip("/").endswith("/cancellation")]) == 1
    r = _refund(world, "aftersales", "seller_cancel", **{**body, "store_id": OTHER, "order_id": "22-22222-22222",
                                                          "confirm_order_id": "22-22222-22222", "conversation_id": None})
    assert r.status_code == 403 and "eBay 后台" in _detail(r), r.text


def test_seller_cancel_confirm_order_mismatch(world, ebay):
    body = {"store_id": LIVE, "order_id": ORDER2, "reason": "OUT_OF_STOCK_OR_CANNOT_FULFILL",
            "confirm_order_id": "99-99999-99999"}
    r = _refund(world, "aftersales", "seller_cancel", **body)
    assert r.status_code in (409, 422), r.text
    assert not [c for c in ebay.writes if c.path.rstrip("/").endswith("/cancellation")]


# ================================================================ 9. 纠纷不做“上传运单号”（1 号说明：eBay Case Management 没这个接口）
def test_case_shipment_not_supported(world, ebay):
    cid = world.cases[(LIVE, "CASE")]
    r = world.admin.post(f"/internal/ebay-cases/{cid}/case/shipment", json={"tracking_number": "X", "carrier": "DHL"})
    assert r.status_code == 422 and "没有这种处理" in _detail(r), r.text
    assert ebay.writes == []



# ================================================================ 7b. 退货 / 纠纷退款走退款申请单（execution=post_order_return，2026-11-12 产品定）
@pytest.mark.parametrize("target", ["return", "case"])
@pytest.mark.parametrize("who", ["admin", "cs"])
def test_direct_return_case_refund_redirected(world, ebay, target, who):
    cid = world.cases[(LIVE, TARGETS[target]["kind"])]
    need(world.admin.app, "POST", TARGETS[target]["route"].format(id="{}", action="refund"), "1 号")
    r = getattr(world, who).post(f"/internal/ebay-cases/{cid}/{target}/refund",
                                 json={"mode": "full", "amount": ORDER_TOTAL, "currency": "EUR", "confirm": True})
    assert r.status_code in (403, 409) and "退款申请" in _detail(r), r.text
    assert ebay.writes == []


def _return_refunds(ebay):
    return [c for c in ebay.writes if c.path.startswith("/post-order/v2/return/") and c.path.endswith("/issue_refund")]


def _row(rid):
    from app.db import session_scope
    from app.models import RefundRequest

    with session_scope() as db:
        r = db.get(RefundRequest, rid)
        return r.status, r.execution, r.return_id


@pytest.mark.parametrize("kind", ["RETURN", "CASE"], ids=["return", "case_escalated"])
def test_return_refund_via_request_end_to_end(world, ebay, kind):
    from app import aftersales_reasons as ar
    from app.db import session_scope
    from app.models import OrderAftersalesReason as R

    r = _refund(world, "cs", "create", **return_refund_body(world, kind))
    rid = _ok_id(r)
    status, execution, return_id = _row(rid)
    assert (status, execution, return_id) == ("PENDING", "post_order_return", "R7RET1R7A"), (status, execution, return_id)
    assert ebay.writes == []  # 默认先审核
    assert _refund(world, "cs", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR").status_code == 403
    ok = _refund(world, "aftersales", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR")
    assert ok.status_code < 400, ok.text
    assert len(_return_refunds(ebay)) == 1 and not [c for c in ebay.writes if "/sell/fulfillment/" in c.path]  # 通过退货退，不按订单退
    assert _row(rid)[0] in ("REFUNDING", "REFUNDED")
    _refund(world, "aftersales", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR")  # 重复点
    assert len(_return_refunds(ebay)) == 1
    ar.refresh_store(LIVE)  # D3：退款中 / 已退款的申请单进售后原因表的退款信号
    with session_scope() as db:
        row = db.query(R).filter_by(store_id=LIVE, order_id=world.order_id).one_or_none()
        assert row is not None and row.refunded, "D3：退款后售后原因表要标退款"


def test_return_partial_refund_via_request(world, ebay):
    rid = _ok_id(_refund(world, "cs", "create", **return_refund_body(world, mode="partial", amount=30.0)))
    ok = _refund(world, "aftersales", "approve", rid, confirm_amount=30.0, confirm_currency="EUR")
    assert ok.status_code < 400, ok.text
    calls = _return_refunds(ebay)
    assert len(calls) == 1 and str(30.0) in str(calls[0].body), calls[0].body if calls else None


@pytest.mark.parametrize("amount", [ORDER_TOTAL + 1, 5.0], ids=["over_ebay_full", "under_ebay_min"])
def test_return_refund_amount_checked_at_execution(world, ebay, amount):
    """全额要等于 eBay 估算的全额；部分要在 eBay 给的 min / max 之间（样例：商品 10–45 + 运费 5）。"""
    mode = "full" if amount > ORDER_TOTAL else "partial"
    r = _refund(world, "cs", "create", **return_refund_body(world, mode=mode, amount=amount))
    if r.status_code >= 400:  # 提交时就拦下也可以
        assert r.status_code in (409, 422), r.text
    else:
        rid = r.json()["id"]
        r = _refund(world, "aftersales", "approve", rid, confirm_amount=amount, confirm_currency="EUR")
        assert r.status_code in (409, 422), r.text
        assert _row(rid)[0] == "PENDING"  # 执行前没过：留在待审核
    assert not _return_refunds(ebay)


def test_case_refund_checks_order_refunded_d1(world, ebay):
    """D1：纠纷（转到退货）退款执行前也查订单，eBay 已全额退款 → 409，不调 issue_refund，申请单留在待审核。"""
    ebay.on("GET", r"/sell/fulfillment/v1/order", 200, {"orderPaymentStatus": "FULLY_REFUNDED", "orderId": "11-11111-11111",
                                                        "pricingSummary": {"total": {"value": "50.00", "currency": "EUR"}},
                                                        "paymentSummary": {"refunds": [{"amount": {"value": "50.00"}}]}})
    r = _refund(world, "cs", "create", **return_refund_body(world, "CASE"))
    if r.status_code >= 400:
        assert r.status_code == 409, r.text
    else:
        rid = r.json()["id"]
        r = _refund(world, "aftersales", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR")
        assert r.status_code == 409, r.text
        assert _row(rid)[0] == "PENDING"
    assert not _return_refunds(ebay)


@pytest.mark.parametrize("failure", ["timeout", "503"])
def test_return_refund_failure_audited_d2(world, ebay, failure):
    rid = _ok_id(_refund(world, "cs", "create", **return_refund_body(world)))
    ebay.fail_writes(exc=httpx.ReadTimeout("t")) if failure == "timeout" else ebay.fail_writes(status=503)
    before = last_audit_id()
    r = _refund(world, "aftersales", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR")
    assert r.status_code == 502 or r.status_code < 400 and _status(r) == "ERROR", r.text
    assert _row(rid)[0] == "ERROR"  # 处理异常
    _refund(world, "aftersales", "approve", rid, confirm_amount=ORDER_TOTAL, confirm_currency="EUR")  # 不能再点通过重发
    assert len(ebay.writes) == 1
    new = audits(LIVE, before)
    assert any(a[0].endswith("_FAILED") for a in new), f"D2：失败要有审计：{new}"


def test_return_refund_rule_direct(world, ebay):
    assert _refund(world, "admin", "rules_save", **refund_rules_body(LIVE, 0, 100)).status_code < 400
    r = _refund(world, "aftersales", "create", **return_refund_body(world))
    assert r.status_code < 400, r.text
    assert len(_return_refunds(ebay)) == 1  # 命中直接退款规则、售后提交：马上通过退货退
    assert _row(r.json()["id"])[0] in ("REFUNDING", "REFUNDED")
