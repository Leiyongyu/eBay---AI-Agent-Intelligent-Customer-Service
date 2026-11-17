"""取消交易在本系统里处理（Post-Order cancellation，真实写操作，用店铺自己的令牌）：

  approve  同意买家的取消申请   POST /post-order/v2/cancellation/{cancelId}/approve   无请求体（eBay 随后自动全额退款）
  reject   拒绝买家的取消申请   POST /post-order/v2/cancellation/{cancelId}/reject    {shipmentDate, trackingNumber}（都可选）
           eBay 不收拒绝原因：原因记在本系统（处理备注、审计、会话事件），已发货的把发货日期 / 运单号带给 eBay
  create   卖家主动取消订单     POST /post-order/v2/cancellation                       {legacyOrderId, cancelReason, buyerPaid}
           原因只能是 OUT_OF_STOCK_OR_CANNOT_FULFILL / BUYER_ASKED_CANCEL / ADDRESS_ISSUES
  check    能不能取消（只读）   POST /post-order/v2/cancellation/check_eligibility     {legacyOrderId}

同意、主动取消会退款给买家：只给管理员和售后；拒绝：能看这个店铺的账号都可以。只对对接 eBay 真实收发的店铺开放。
EU/UK 卖家的“创建 / 同意取消”要数字签名（ebay/signing.py，由 case_actions_common.post 统一加）。
共用部分（店铺 / 角色检查、不重试的 post、做成以后的审计 / 处理备注 / 会话系统事件 / 售后状态）在 case_actions_common。
给买家的通知不自动发：会话里有系统事件，客服用 AI 起草后人工审核发送。
"""
from __future__ import annotations

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import case_actions_common as cac
from .case_actions_common import ActionError as CancelError  # noqa: F401  接口层按 status_code / message 处理
from .db import utcnow
from .models import AuditEvent, Conversation, EbayCase, EbayOrder, Store

SELLER_REASONS = {"OUT_OF_STOCK_OR_CANNOT_FULFILL": "缺货 / 无法发货", "BUYER_ASKED_CANCEL": "买家要求取消",
                  "ADDRESS_ISSUES": "收货地址有问题"}
ACTIONS = {"approve": "同意取消", "reject": "拒绝取消"}  # 纠纷处理页（买家的取消申请）里的处理
CREATE_LABEL = "主动取消订单"  # 会话侧栏订单发起
SENIOR_ACTIONS = ("approve", "create")
ACTOR = {"BUYER": "买家", "SELLER": "卖家", "SYSTEM": "系统", "CSR": "eBay 客服"}
MAX_TEXT = 500


def allowed(user, action: str) -> bool:
    return cac.allowed(user, action, SENIOR_ACTIONS)


def _post(store_id: str, path: str, body: dict, http, endpoint: str, user, *, case: EbayCase | None = None,
          action: str = "") -> dict:
    """case_actions_common.post。调 eBay 失败时（超时 / 5xx / 4xx）先用独立事务记一条审计 CANCEL_<动作>_FAILED
    （接口层之后会回滚，这条要留着：谁、什么时候、哪条、结果是否不确定），数字签名错误再单独记错误码，然后抛出。"""
    try:
        return cac.post(store_id, path, body, http)
    except CancelError as e:
        if getattr(e, "called_ebay", True):  # 请求真的发到了 eBay（发出之前就挡下的不记）
            if case is not None:  # 和 1 号的咨询 / 退货 / 纠纷同一格式：{KIND}_{ACTION}_FAILED + 处理备注
                cac.record_failure(case, action, e, cac.who(user))
            else:  # 主动取消：还没有取消记录，按同样字段只记审计
                _failed_audit(store_id, None, action, path, e, user)
        from .ebay.signing import signature_error_code
        from .refund_requests import record_signature_error

        code = getattr(e, "signature_code", None) or signature_error_code(e.message)             or ("215xxx" if "数字签名" in (e.message or "") else None)  # 公共 post 只给中文提示时：错误码未知
        if code:
            record_signature_error(store_id, endpoint, code, cac.who(user))
        raise


def _failed_audit(store_id: str, case: EbayCase | None, action: str, path: str, e, user) -> None:
    from .db import session_scope

    try:
        with session_scope() as s:
            s.add(AuditEvent(actor=cac.who(user), action=f"CANCEL_{action.upper()}_FAILED", store_id=store_id,
                             detail={"case_id": case.id if case else None, "external_id": case.external_id if case else None,
                                     "endpoint": path, "status": e.status_code, "error": e.message,
                                     "uncertain": e.status_code == 502,  # 超时 / 5xx：已尝试，结果不确定
                                     **({"signature_error_code": e.signature_code} if getattr(e, "signature_code", None) else {})}))
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("记取消失败审计失败：%s %s", store_id, path)


def parse_detail(d: dict) -> dict:
    """eBay 取消详情（GET /post-order/v2/cancellation/{cancelId}）里纠纷处理页要用的部分。"""
    det = d.get("cancelDetail") or d
    hist = []
    for h in det.get("activityHistories") or det.get("activityHistory") or []:
        hist.append({"actor": ACTOR.get(h.get("actionBy") or h.get("actor"), h.get("actionBy") or h.get("actor")),
                     "action": h.get("activityType") or h.get("action"), "description": h.get("description"),
                     "at": cac.dt(h.get("creationDate") or h.get("actionDate"))})
    hist.sort(key=lambda h: h["at"] or "")
    return {"state": det.get("cancelState"), "status": det.get("cancelStatus"), "reason": det.get("cancelReason"),
            "requestor": det.get("requestorType"), "close_reason": det.get("cancelCloseReason"),
            "payment_status": det.get("paymentStatus"), "refund_amount": cac.amount(det.get("requestRefundAmount")),
            "requested_at": cac.dt(det.get("cancelRequestDate")), "due_at": cac.dt(det.get("sellerResponseDueDate")),
            "closed_at": cac.dt(det.get("cancelCloseDate")), "order_id": det.get("legacyOrderId"), "history": hist}


def live_detail(store_id: str, cancel_id: str, api=None) -> dict:
    """实时查一条取消申请（只读）。"""
    return parse_detail(cac.get_detail(store_id, f"/post-order/v2/cancellation/{cancel_id}", api))


def can_do(live: dict | None, case: EbayCase) -> dict[str, bool]:
    """eBay 现在允不允许同意 / 拒绝：本地记录还开着、eBay 上没关闭、是买家发起的申请（实时详情拿不到时只看本地）。"""
    ok = bool(case.is_open)
    if live is not None:
        ok = ok and (live.get("state") or "").upper() != "CLOSED" and (live.get("requestor") or "BUYER").upper() == "BUYER"
    return {a: ok for a in ACTIONS}


def respond(db: Session, case: EbayCase, action: str, payload: dict, user, *, http: httpx.Client | None = None) -> dict:
    """同意 / 拒绝买家的取消申请。"""
    from .auth import check_store

    check_store(case.store_id)
    if action not in ("approve", "reject"):
        raise CancelError(422, "没有这种处理")
    cac.check(db, case, "CANCEL", action, ACTIONS, SENIOR_ACTIONS, user)
    if action == "approve":
        if str(payload.get("confirm_order_id") or "").strip() != (case.order_id or ""):
            raise CancelError(409, "确认的订单号和这条取消申请对不上，请刷新后再确认")
        body: dict = {}
        summary = f"已同意买家取消订单 {case.order_id}（原因：{case.reason or '未说明'}），eBay 会自动全额退款给买家"
    else:
        why = cac.text(payload.get("reason"), "拒绝原因", True, MAX_TEXT)
        body = {}
        shipped = cac.iso_date(payload.get("shipped_at"), "发货时间")
        if shipped:
            body["shipmentDate"] = shipped
        tracking = cac.text(payload.get("tracking_number"), "运单号", False, 64)
        if tracking:
            body["trackingNumber"] = tracking
        ship = f"，已发货{('，运单 ' + tracking) if tracking else ''}" if body else ""
        summary = f"已拒绝买家取消订单 {case.order_id} 的申请（{why}{ship}），订单继续履约"
    _post(case.store_id, f"/post-order/v2/cancellation/{case.external_id}/{action}", body, http, f"cancellation.{action}", user,
          case=case, action=action)
    cac.record_action(db, case, action=action, label=ACTIONS[action], summary=summary, user=user, work_state="DONE")
    if action == "approve":  # 同意取消 eBay 自动全额退款：本地订单马上标已退款 / 已取消，售后原因表不用等财务流水
        cac.mark_order_refunded(db, case)
        o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == case.store_id, EbayOrder.order_id == case.order_id))
        if o is not None:
            o.cancelled = True
    now = utcnow()
    case.is_open = False
    case.status = ("CANCEL_APPROVED_BY_SELLER" if action == "approve" else "CANCEL_REJECTED_BY_SELLER")[:64]
    case.status_changed_at = now
    db.flush()
    return {"done": summary, "status": case.status, "is_open": case.is_open, "work_state": case.work_state}


def check_eligibility(db: Session, store_id: str, order_id: str, *, http: httpx.Client | None = None) -> dict:
    """能不能主动取消（只读，eBay 实时）。"""
    d = cac.post(store_id, "/post-order/v2/cancellation/check_eligibility", {"legacyOrderId": order_id}, http)
    eligible = d.get("eligible")
    reasons = d.get("failureReason") or d.get("failureReasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    return {"eligible": bool(eligible) if eligible is not None else None, "reasons": reasons,
            "eligible_cancel_reasons": d.get("eligibleCancelReason") or d.get("eligibleCancelReasons")}


def create(db: Session, user, payload: dict, *, http: httpx.Client | None = None) -> dict:
    """卖家主动取消订单（从会话侧栏订单发起）。eBay 返回 cancelId 后先在本地记一条取消记录（下次同步按编号更新），
    再按这条记录走 record_action（审计、处理备注、会话系统事件、会话售后状态）。"""
    from .auth import check_store

    store_id = payload.get("store_id")
    order_id = str(payload.get("order_id") or "").strip()
    conv = db.get(Conversation, payload["conversation_id"]) if payload.get("conversation_id") else None
    if conv is not None:
        store_id = store_id or conv.store_id
        if conv.store_id != store_id:
            raise CancelError(422, "店铺和会话对不上")
    if not store_id or not order_id:
        raise CancelError(422, "请选择订单")
    check_store(store_id)
    if not allowed(user, "create"):
        raise CancelError(403, f"{CREATE_LABEL}只有管理员和售后可以做")
    reason = cac.cannot_act(db.get(Store, store_id))
    if reason:
        raise CancelError(403, reason)
    code = payload.get("reason")
    if code not in SELLER_REASONS:
        raise CancelError(422, "请选择取消原因")
    if str(payload.get("confirm_order_id") or "").strip() != order_id:
        raise CancelError(409, "确认的订单号对不上，请重新确认")
    o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == store_id, EbayOrder.order_id == order_id))
    if o is not None and o.cancelled:
        raise CancelError(409, "这张订单已经取消了")
    if db.scalar(select(EbayCase.id).where(EbayCase.store_id == store_id, EbayCase.kind == "CANCEL",
                                           EbayCase.order_id == order_id, EbayCase.is_open.is_(True))):
        raise CancelError(409, "这张订单已经有一条没处理完的取消申请，请在“取消交易”里处理")
    note = cac.text(payload.get("note"), "备注", False, MAX_TEXT)
    d = _post(store_id, "/post-order/v2/cancellation", {"legacyOrderId": order_id, "cancelReason": code, "buyerPaid": True}, http,
              "cancellation.create", user, action="create")
    cancel_id = str(d.get("cancelId") or "").strip() or f"SELLER-{order_id}"
    now = utcnow()
    actor = cac.who(user)
    buyer = (o.buyer if o else None) or (conv.buyer_key if conv else None)
    case = db.scalar(select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.kind == "CANCEL",
                                            EbayCase.external_id == cancel_id))
    if case is None:
        case = EbayCase(store_id=store_id, kind="CANCEL", external_id=cancel_id, order_id=order_id, buyer=buyer,
                        status="CANCEL_REQUESTED_BY_SELLER", is_open=False, opened_at=now, reason=code,
                        amount=o.total if o else None, currency=o.currency if o else None,
                        detail={"requestor": "SELLER", "created_in_system": True},
                        first_seen_at=now, last_seen_at=now, status_changed_at=now, seen_at=now, seen_by=actor)
        db.add(case)
        db.flush()
    summary = f"已主动取消订单 {order_id}（原因：{SELLER_REASONS[code]}{('；' + note) if note else ''}），eBay 会自动退款给买家"
    cac.record_action(db, case, action="create", label=CREATE_LABEL, summary=summary, user=user, work_state="DONE")
    if o is not None:
        o.cancelled = True
        o.refunded = True  # 卖家取消 eBay 自动退款
    db.flush()
    return {"done": summary, "cancel_id": None if cancel_id.startswith("SELLER-") else cancel_id, "case_id": case.id}


def order_store_for(db: Session, conversation_id: int | None, store_id: str | None) -> str | None:
    if store_id:
        return store_id
    if conversation_id:
        c = db.get(Conversation, conversation_id)
        return c.store_id if c else None
    return None


def buyer_orders(db: Session, store_id: str, buyer: str | None) -> list[str]:
    if not buyer:
        return []
    return [o for o in db.scalars(select(EbayOrder.order_id).where(EbayOrder.store_id == store_id,
                                                                    func.lower(EbayOrder.buyer) == buyer.lower())
                                  .order_by(EbayOrder.created_at.desc()).limit(20))]
