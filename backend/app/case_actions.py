"""售后管理 → 纠纷（eBay 介入的 case：未收到货升级、退货升级）的“纠纷处理”页。

eBay 现在的 Case Management API 只有 getCase、search、appeal 三个接口（2026-11 核对文档：casemanagement 的
issue_refund / provide_shipment_info / provide_return_address 已从文档移除，纠纷本身也没有发信、上传证据的接口），所以：
  - 详情：GET /post-order/v2/casemanagement/{caseId}（状态、金额、关闭原因、能否申诉、处理历史、运单）；
  - appeal  申诉裁决   POST /casemanagement/{caseId}/appeal（eBay 说可以申诉 eligibleForAppeal 时；管理员、售后）；
  - 退货升级来的纠纷（getCase 有 returnId）：refund 退款 / message 发信 / file 上传证据转给那条退货（return_actions.run），
    能不能做按那条退货现在的 sellerAvailableOptions（eBay 介入后通常不再开放，就提示到 eBay 后台）；
  - 未收到货升级来的纠纷：除申诉外没有接口，提示到 eBay 后台（上传运单号、POD 都在 eBay 后台做）。
纠纷结果（买家 WIN / 卖家 WIN）：按 buyerClosureReason / sellerClosureReason 判定，存进 ebay_cases.detail.outcome；
已关闭的纠纷在同步时补查（fill_outcomes，只读，每店每轮有上限）。
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import case_actions_common as common
from . import return_actions
from .case_actions_common import ActionError
from .db import utcnow
from .models import EbayCase

log = logging.getLogger(__name__)

ACTIONS = {"appeal": "申诉裁决", "refund": "退款", "message": "给买家发信", "file": "上传证据"}
SENIOR_ACTIONS = ("appeal",)  # 退款走退款申请单（客服也能提交，审核 / 执行由申请单那边检查）
OUTCOME_LABEL = {"BUYER": "买家 WIN", "SELLER": "卖家 WIN"}
FILL_LIMIT = 20  # 同步时每个店铺每轮最多补查几条已关闭纠纷的结果
NO_API = "eBay 没有给纠纷开放这个接口（只能查询和申诉），eBay 没有开放接口，请到 eBay 后台处理"


def allowed(user, action: str) -> bool:
    return common.allowed(user, action, SENIOR_ACTIONS)


def outcome(buyer_reason: str | None, seller_reason: str | None) -> str | None:
    """纠纷结果：SELLER 卖家赢 / BUYER 买家赢（含买家拿到退款）/ None 判不出（还开着、取消等）。"""
    reasons = " ".join(str(x or "").upper() for x in (buyer_reason, seller_reason))
    if "SELLER_WIN" in reasons:
        return "SELLER"
    if "BUYER_WIN" in reasons or "BUYER_REFUNDED" in reasons or "_REFUND_" in reasons or reasons.endswith("_REFUND"):
        return "BUYER"
    return None


def parse_detail(d: dict) -> dict:
    det = d.get("caseDetails") or {}
    refunds = det.get("refundAmounts") or {}
    hist = d.get("caseHistoryDetails") or {}
    track = hist.get("shipmentTrackingDetails") or {}
    appeal = d.get("appealDetails") or {}
    history = [{"actor": h.get("actor"), "action": h.get("action"), "description": h.get("description"),
                "at": common.dt(h.get("date"))} for h in hist.get("history") or []]
    history.sort(key=lambda h: h["at"] or "")
    br, sr = d.get("buyerClosureReason"), d.get("sellerClosureReason")
    oc = outcome(br, sr)
    return {
        "case_id": str(d.get("caseId") or ""), "status": d.get("status"), "case_type": d.get("caseType"),
        "return_id": str(d["returnId"]) if d.get("returnId") else None,
        "claim_amount": common.amount(d.get("claimAmount")), "escalated_by": d.get("escalatedBy"),
        "escalate_reason": d.get("escalateReason"), "initiator": d.get("initiator"),
        "expected": det.get("buyerInitExpectResolution"), "final": det.get("buyerFinalAcceptResolution"),
        "expected_refund": common.amount(refunds.get("buyerInitExpectRefundAmt")),
        "refunded": common.amount(refunds.get("refundAmount")),
        "opened_at": common.dt(det.get("creationDate") or d.get("creationDate")),
        "escalated_at": common.dt(det.get("escalationDate")), "expires_at": common.dt(det.get("expirationDate")),
        "refund_deadline": common.dt(det.get("refundDeadlineDate")),
        "buyer_closure_reason": br, "seller_closure_reason": sr, "outcome": oc, "outcome_label": OUTCOME_LABEL.get(oc or ""),
        "ebay_url": d.get("buyerProtectedProgramLink"),
        "appeal": {"eligible": bool(appeal.get("eligibleForAppeal")), "status": appeal.get("appealStatusEnum") or appeal.get("appealStatus"),
                   "at": common.dt(appeal.get("appealDate"))} if appeal else {"eligible": False, "status": None, "at": None},
        "tracking": {"carrier": track.get("carrier"), "number": track.get("trackingNumber"),
                     "status": track.get("currentStatus")} if track else None,
        "history": history,
    }


def live_detail(store_id: str, case_id: str, api=None) -> dict:
    from .sources.ebay import EbayReadOnlyApi

    try:
        d = common.get_detail(store_id, f"/post-order/v2/casemanagement/{case_id}", api or EbayReadOnlyApi(store_id))
    except ActionError as e:
        if e.status_code == 404:
            raise ActionError(404, "eBay 上找不到这条纠纷") from None
        raise
    return parse_detail(d)


def save_outcome(case: EbayCase, live: dict) -> None:
    """把结果记进 detail（列表的“纠纷结果”筛选用）。"""
    case.detail = {**(case.detail or {}), "outcome": live.get("outcome"),
                   "closure_reason": live.get("seller_closure_reason") or live.get("buyer_closure_reason"),
                   "return_id": live.get("return_id"), "case_type": live.get("case_type"),
                   "outcome_checked_at": utcnow().isoformat() + "Z"}


def can_do(live: dict | None, return_live: dict | None) -> dict[str, bool]:
    if live is None:
        return {a: False for a in ACTIONS}
    rcan = return_actions.can_do(return_live) if return_live is not None else {}
    has_return = bool(live.get("return_id"))
    ropts = set((return_live or {}).get("options") or [])
    return {"appeal": bool(live["appeal"]["eligible"]),
            "refund": has_return and bool(rcan.get("refund")),
            # eBay 介入后对应退货通常不再开放留言：按那条退货的下一步里有没有“给买家留言”
            "message": has_return and (return_live is None or "SELLER_SEND_MESSAGE" in ropts),
            "file": has_return}


def blocked_reason(live: dict | None, action: str) -> str | None:
    if live is None:
        return "eBay 纠纷详情没取到"
    if action == "appeal":
        return None if live["appeal"]["eligible"] else "eBay 显示这条纠纷现在不能申诉"
    if not live.get("return_id"):
        return NO_API
    return None


def platform_reason(live: dict | None, return_live: dict | None, action: str) -> str | None:
    """纠纷上这个处理为什么做不了（eBay 那边的原因；None = eBay 允许）。"""
    why = blocked_reason(live, action)
    if why or action in ("appeal", "file"):
        return why
    can = can_do(live, return_live)
    if can.get(action):
        return None
    nxt = "、".join((return_live or {}).get("option_labels") or []) or "没有"
    what = {"refund": "退款", "message": "留言"}.get(action, ACTIONS[action])
    return f"eBay 介入后没有给卖家开放{what}接口（对应退货 eBay 给卖家的下一步：{nxt}），请到 eBay 后台处理"


def perform(db: Session, case: EbayCase, action: str, payload: dict, user, *, http: httpx.Client | None = None,
            api=None) -> dict:
    common.check(db, case, "CASE", action, ACTIONS, SENIOR_ACTIONS, user, open_required=action not in ("message", "appeal"))
    live = live_detail(case.store_id, case.external_id, api)
    save_outcome(case, live)
    why = blocked_reason(live, action)
    if why:
        raise ActionError(409, why)
    if action == "message":  # eBay 介入后对应退货不开放留言：发件箱也不收，免得到期才失败
        rlive = return_actions.live_detail(case.store_id, live["return_id"], api)
        why = platform_reason(live, rlive, action)
        if why:
            raise ActionError(409, why)
    if action != "appeal":  # 退货升级来的纠纷：转到那条退货
        r = return_actions.run(db, case, live["return_id"], action, payload, user, http=http, api=api)
        r["case_live"] = live
        return r
    note = common.text(payload.get("comment"), "申诉理由", True)
    common.post(case.store_id, f"/post-order/v2/casemanagement/{case.external_id}/appeal", {"comments": {"content": note}}, http)
    common.record_action(db, case, action="appeal", label=ACTIONS["appeal"], summary="申诉 eBay 的裁决：" + note[:60], user=user)
    try:
        live = live_detail(case.store_id, case.external_id, api)
        save_outcome(case, live)
    except Exception as e:  # noqa: BLE001
        log.warning("申诉后刷新纠纷 %s 失败：%s", case.external_id, e)
    db.flush()
    return {"done": "已提交申诉", "live": live, "case_live": live}


def fill_outcomes(db: Session, store_id: str, api=None, limit: int = FILL_LIMIT) -> int:
    """同步时补查：已关闭、还没查过结果的纠纷调 getCase（只读）记下结果。返回补查条数。"""
    rows = [c for c in db.scalars(select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.kind == "CASE",
                                                         EbayCase.is_open.is_(False))
                                  .order_by(EbayCase.opened_at.desc()).limit(500))
            if "outcome_checked_at" not in (c.detail or {})][:limit]
    n = 0
    for c in rows:
        try:
            save_outcome(c, live_detail(store_id, c.external_id, api))
            n += 1
        except Exception as e:  # noqa: BLE001
            log.info("补查纠纷 %s 结果失败：%s", c.external_id, e)
            break  # 多半是令牌或网络问题：这一轮不再查
    db.flush()
    return n
