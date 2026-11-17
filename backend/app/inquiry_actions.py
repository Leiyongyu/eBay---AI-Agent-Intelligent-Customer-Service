"""售后管理 → 咨询（未收到货 INR）的“纠纷处理”页：实时详情（处理历史、eBay 记录的运单）和四种处理。

四种处理都是真实写操作（Post-Order API，用店铺自己的令牌），只给对接 eBay 真实收发的店铺（EBAY_LIVE_STORES）、
发送渠道是 eBay（EBAY_CHANNEL=ebay）时做；其他店铺提示到 eBay 后台处理：
  shipment  上传运单号   POST /post-order/v2/inquiry/{id}/provide_shipment_info
            trackingNumber、shippingCarrierName、shippingDate、shippedWithTracking、sellerComments
  escalate  升级为纠纷   POST /post-order/v2/inquiry/{id}/escalate    escalateInquiryReason、comments（必填）
  refund    全额退款     POST /post-order/v2/inquiry/{id}/issue_refund comments；退的是咨询的索赔金额（全额），
            要求前端把确认的金额原样带回，和 eBay 上的索赔金额不一致就不提交
  message   给买家发信   POST /post-order/v2/inquiry/{id}/send_message message——不直接发：走发件箱（合规检查、审核记录、
            5 分钟可撤回），到期后发件箱调这个接口（case_actions_common.queue_message）
权限：上传运单号、发信——能看这个店铺的账号都可以；升级、退款——只有管理员和售后。
超时 / 5xx 不重试（不确定 eBay 有没有收到），提示到 eBay 后台核对；成功后刷新这条咨询的状态，记审计、处理备注、
会话系统事件和会话售后状态（case_actions_common.record_action）。
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from . import case_actions_common as common
from .case_actions_common import ActionError as InquiryError  # noqa: F401  （旧名字，测试和接口层在用）
from .case_actions_common import cannot_act  # noqa: F401
from .models import EbayCase

log = logging.getLogger(__name__)

ACTIONS = {"shipment": "上传运单号", "escalate": "升级为纠纷", "refund": "全额退款", "message": "给买家发信"}
# 卖家能用的升级原因（InquiryEscalationReasonEnum 里卖家可用的）
ESCALATE_REASONS = {
    "SHIPPED_ITEM": "已发货，买家仍说没收到",
    "BUYER_TROUBLE": "和买家处理不下去（买家不配合）",
    "TROUBLE_COMMUNICATION": "和买家沟通有困难",
    "OTHERS": "其他原因",
}
SENIOR_ACTIONS = ("escalate", "refund")


def allowed(user, action: str) -> bool:
    return common.allowed(user, action, SENIOR_ACTIONS)


_dt, _amount, _text = common.dt, common.amount, common.text


def parse_detail(d: dict) -> dict:
    """eBay 咨询详情（GET /post-order/v2/inquiry/{id}）里纠纷处理页要用的部分。"""
    det = d.get("inquiryDetails") or {}
    refunds = det.get("refundAmounts") or {}
    hist = (d.get("inquiryHistoryDetails") or {})
    track = hist.get("shipmentTrackingDetails") or {}
    history = [{"actor": h.get("actor"), "action": h.get("action"), "description": h.get("description"),
                "at": _dt(h.get("date"))} for h in hist.get("history") or []]
    history.sort(key=lambda h: h["at"] or "")
    return {
        "status": d.get("inquiryStatusEnum") or d.get("status"), "state": d.get("state"),
        "creation_reason": d.get("creationReason"), "initiator": d.get("initiator"),
        "claim_amount": _amount(d.get("claimAmount")), "quantity": d.get("inquiryQuantity"),
        # eBay 记录的退款金额：买家开咨询时期待的、实际已退的、买家最终接受的（没有就是 None）
        "expected_refund": _amount(refunds.get("buyerInitExpectRefundAmt")),
        "refunded": _amount(refunds.get("refundAmount")),
        "final_refund": _amount(refunds.get("buyerFinalAcceptRefundAmt")),
        "opened_at": _dt(det.get("creationDate")), "escalated_at": _dt(det.get("escalationDate")),
        "expires_at": _dt(det.get("expirationDate")), "last_buyer_reply_at": _dt(det.get("lastBuyerRespDate")),
        "closed_at": _dt(d.get("inquiryClosureDate")),
        "tracking": {"carrier": track.get("carrier"), "number": track.get("trackingNumber"),
                     "status": track.get("currentStatus"), "url": track.get("trackingURL")} if track else None,
        "history": history,
    }


def live_detail(store_id: str, inquiry_id: str, api=None) -> dict:
    """实时查一条咨询的详情（只读）。"""
    from .sources.ebay import EbayReadOnlyApi

    try:
        d = common.get_detail(store_id, f"/post-order/v2/inquiry/{inquiry_id}", api or EbayReadOnlyApi(store_id))
    except InquiryError as e:
        if e.status_code == 404:
            raise InquiryError(404, "eBay 上找不到这条咨询") from None
        raise
    return parse_detail(d)


PAYMENT_STATUS, order_refunds, order_refund_state = common.PAYMENT_STATUS, common.order_refunds, common.order_refund_state


def build_request(case: EbayCase, action: str, p: dict, live: dict | None = None) -> tuple[str, dict, str]:
    """(接口路径, 请求体, 处理说明)。参数不对抛 InquiryError(422)。"""
    base = f"/post-order/v2/inquiry/{case.external_id}"
    if action == "shipment":
        number = _text(p.get("tracking_number"), "运单号", True)
        carrier = _text(p.get("carrier"), "物流渠道", True)
        body: dict = {"trackingNumber": number, "shippingCarrierName": carrier, "shippedWithTracking": True}
        when = common.iso_date(p.get("shipped_at"), "发货时间")
        if when:
            body["shippingDate"] = when
        note = _text(p.get("comment"), "备注")
        if note:
            body["sellerComments"] = {"content": note}
        return f"{base}/provide_shipment_info", body, f"上传运单号 {number}（{carrier}）"
    if action == "escalate":
        reason = p.get("reason")
        if reason not in ESCALATE_REASONS:
            raise InquiryError(422, "请选择升级原因")
        comment = _text(p.get("comment"), "给平台的留言", True)
        return f"{base}/escalate", {"escalateInquiryReason": reason, "comments": {"content": comment}}, \
            f"升级为纠纷：{ESCALATE_REASONS[reason]}"
    if action == "refund":
        if not p.get("confirm"):
            raise InquiryError(422, "退款要先确认金额")
        claim = (live or {}).get("claim_amount")  # 只认 eBay 实时的索赔金额
        want = p.get("amount")
        if claim is None or want is None or abs(float(want) - float(claim["value"])) > 0.005 \
                or (p.get("currency") and claim.get("currency") and p["currency"] != claim["currency"]):
            raise InquiryError(409, "退款金额和 eBay 上的索赔金额对不上，请刷新后再确认")
        note = _text(p.get("comment"), "备注")
        return f"{base}/issue_refund", ({"comments": {"content": note}} if note else {}), \
            f"全额退款 {claim.get('currency') or ''} {float(claim['value']):.2f}".strip()
    if action == "message":
        text = _text(p.get("text"), "发信内容", True)
        return f"{base}/send_message", {"message": {"content": text}}, "给买家发信：" + text[:60]
    raise InquiryError(422, "没有这种处理")


def perform(db: Session, case: EbayCase, action: str, payload: dict, user, *, http: httpx.Client | None = None,
            api=None) -> dict:
    """在 eBay 上处理一条咨询。成功返回 {done, live}（live 为刷新后的详情，刷新失败为 None）；发信返回 {done, queued}。"""
    common.check(db, case, "INQUIRY", action, ACTIONS, SENIOR_ACTIONS, user, open_required=action != "message")
    if action == "message":  # 走发件箱：合规、审核记录、可撤回；到期后发到这条咨询的 send_message
        return {"done": "已放进发件箱", "queued": common.queue_message(
            db, case, f"/post-order/v2/inquiry/{case.external_id}/send_message", payload.get("text"), user,
            text_zh=payload.get("text_zh"), handling_ms=payload.get("handling_ms")), "live": None}
    live = None
    if action == "refund":  # 退款前实时核对：索赔金额、这条咨询有没有退过、订单是不是已经全额退款
        live = live_detail(case.store_id, case.external_id, api)
        if live.get("refunded") and live["refunded"]["value"] > 0:
            raise InquiryError(409, f"eBay 显示这条咨询已经退过款（{live['refunded']['currency'] or ''} "
                                    f"{live['refunded']['value']:.2f}），不能再退")
        try:
            st = order_refund_state(case.store_id, case.order_id)
        except Exception as e:  # noqa: BLE001  查不到订单状态就不退，免得重复退款
            raise InquiryError(502, f"查不到订单的退款情况（{str(e)[:120]}），请稍后再试或到 eBay 后台处理") from None
        if st and st["payment_status"] == "FULLY_REFUNDED":
            raise InquiryError(409, "eBay 显示这笔订单已经全额退款，不能再退")
    path, body, summary = build_request(case, action, payload, live)
    common.post(case.store_id, path, body, http)
    common.record_action(db, case, action=action, label=ACTIONS[action], summary=summary, user=user,
                         work_state="DONE" if action == "refund" else "PROCESSING", aftersales_done=action == "refund")
    if action == "refund":  # 售后原因表马上标退款（接口层提交后重算），不等财务流水同步
        common.mark_order_refunded(db, case)
    try:  # 刷新 eBay 上的状态（失败不影响已经做成的处理）
        live = live_detail(case.store_id, case.external_id, api)
        if live.get("status"):
            from .ebay_cases import _closed

            case.status = str(live["status"])[:64]
            case.is_open = not _closed(live["status"])
    except Exception as e:  # noqa: BLE001
        log.warning("处理后刷新咨询 %s 失败：%s", case.external_id, e)
        live = None
    db.flush()
    return {"done": summary, "live": live}
