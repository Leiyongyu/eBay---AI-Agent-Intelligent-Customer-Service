"""售后管理 → 退货的“纠纷处理”页：实时详情（getReturn）和处理方案。能做哪些处理按 eBay 给卖家的
sellerAvailableOptions 决定，退款的全额 / 部分和金额范围按 eBay 给的每一项 estimatedAmount / minAmount / maxAmount 决定。

处理（真实写操作，只对接 eBay 的店铺；接口见 developer.ebay.com/devzone/post-order）：
  accept    接受退货     ① POST /return/{id}/decide  decision=APPROVE（+ RMANumber；已经同意过、只差 RMA 时 PROVIDE_RMA）
                         ② 按处理方式 POST /return/{id}/add_shipping_label：
                            upload_label  先 POST /return/{id}/file/upload（filePurpose=LABEL_RELATED）拿 fileId，再
                                          labelAction=UPLOAD_LABEL + fileId + trackingNumber + carrierName
                            no_label      labelAction=NO_LABEL + noLabelReason（ITEM_TOO_LARGE / REIMBURSE / RETURN_TO_INTL_LOCATION /
                                          RETURN_TO_STORE / WORK_WITH_BUYER / OTHER）+ 给平台的留言
                            ebay_label    labelAction=EBAY_LABEL + carrierEnum（eBay 生成运输标签）
                            accept_only   只做 ①
                         ① 成功、② 失败时：① 照样记下，返回 partial_error 提示到 eBay 后台补面单
  refund    退款         eBay 给了 SELLER_ISSUE_REFUND：POST /return/{id}/issue_refund，refundDetail.itemizedRefundDetail
                         按 eBay 估算的每一项；部分退款只减“商品价格”这一项，且必须在 eBay 给的 [min, max] 内（min=max 就不能部分退）；
                         只给了 SELLER_OFFER_PARTIAL_REFUND：部分退款走 decide OFFER_PARTIAL_REFUND（买家同意后才退，可选买家留下商品）
  received  确认收到退货 POST /return/{id}/mark_as_received（eBay 给了 SELLER_MARK_AS_RECEIVED 时）
  file      上传文件     POST /return/{id}/file/upload（ITEM_RELATED / LABEL_RELATED / REFUND_RELATED，图片或 PDF）
  message   给买家发信   走发件箱（合规、审核记录、5 分钟可撤回），到期后发件箱调 POST /return/{id}/send_message
权限：接受退货、退款、确认收到只有管理员和售后；上传文件、发信能看这个店铺的账号都可以。
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy.orm import Session

from . import case_actions_common as common
from .case_actions_common import ActionError
from .models import EbayCase

log = logging.getLogger(__name__)

ACTIONS = {"accept": "接受退货", "refund": "退款", "received": "确认收到退货", "file": "上传文件", "message": "给买家发信"}
# 退款不在这里直接做：页面提交退款申请单（2 号的 /internal/refund-requests，客服也能提交），按审核规则审核或直接执行，
# 执行时调 execute_refund_request（审核 / 执行只有管理员和售后，由申请单那边检查）
SENIOR_ACTIONS = ("accept", "received")
EXECUTOR = "post_order_return"  # 退款申请单的执行方式：用退货的 Post-Order 接口退（不是订单的 issueRefund）
REFUND_VIA_REQUEST = "退款要提交退款申请（按退款审核规则先审核或直接执行），请在“申请退款”页签提交"
ACCEPT_METHODS = {"upload_label": "上传退货面单", "no_label": "无法提供退货标签", "ebay_label": "使用 eBay 运输标签",
                  "accept_only": "仅接受退货"}
NO_LABEL_REASONS = {
    "ITEM_TOO_LARGE": "商品太大，没法用退货标签寄",
    "REIMBURSE": "不提供标签，退货运费由我们报销",
    "RETURN_TO_INTL_LOCATION": "退货地址在国外",
    "RETURN_TO_STORE": "请买家退回门店",
    "WORK_WITH_BUYER": "和买家商量退货运费",
    "OTHER": "其他原因",
}
FILE_PURPOSES = {"ITEM_RELATED": "商品相关（照片等）", "LABEL_RELATED": "面单 / 物流相关", "REFUND_RELATED": "退款相关"}
# eBay 运输标签可选的承运商（ShippingCarrierEnum）
EBAY_LABEL_CARRIERS = ("ROYAL_MAIL", "PARCEL_FORCE", "HERMES", "DHL", "DEUTSCHE_POST", "UPS", "FEDEX", "USPS",
                       "AUSTRALIA_POST", "CANADA_POST", "LA_POSTE", "COLISSIMO", "CORREOS", "POSTE", "OTHER")
FEE_LABEL = {"PURCHASE_PRICE": "商品价格", "ORIGINAL_SHIPPING": "原运费", "RETURN_SHIPPING": "退货运费",
             "RESTOCKING_FEE": "补货费", "ITEM_TAX": "商品税", "SHIPPING_TAX": "运费税", "OTHER": "其他"}
# eBay 给卖家的下一步 → 本系统哪个处理
OPTION_FOR = {"accept": ("SELLER_APPROVE_REQUEST", "SELLER_PROVIDE_LABEL", "SELLER_PROVIDE_RMA"),
              "refund": ("SELLER_ISSUE_REFUND", "SELLER_OFFER_PARTIAL_REFUND"),
              "received": ("SELLER_MARK_AS_RECEIVED",)}
OPTION_LABEL = {
    "SELLER_APPROVE_REQUEST": "同意退货", "SELLER_DECLINE_REQUEST": "拒绝退货", "SELLER_ESCALATE": "升级",
    "SELLER_ISSUE_REFUND": "退款", "SELLER_OFFER_PARTIAL_REFUND": "提出部分退款", "SELLER_MARK_AS_RECEIVED": "确认收到退货",
    "SELLER_PROVIDE_LABEL": "提供退货标签", "SELLER_PROVIDE_RMA": "提供 RMA 号", "SELLER_SEND_MESSAGE": "给买家留言",
    "SELLER_PRINT_SHIPPING_LABEL": "打印运输标签", "SELLER_VOID_LABEL": "作废标签", "SELLER_RETRY_REFUND": "重试退款",
    "SELLER_MARK_REFUND_SENT": "标记已退款", "SELLER_OFFER_RETURNS": "提供退货", "SELLER_PROVIDE_TRACKING_INFO": "提供运单",
    "SELLER_UPDATE_TRACKING": "更新运单", "SELLER_OFFER_REPLACEMENT": "提供换货", "SELLER_MARK_REPLACEMENT_SHIPPED": "换货已发出",
    "SELLER_ISSUE_STORE_CREDIT_REFUND": "退店铺余额",
}
EPS = 0.005


def allowed(user, action: str) -> bool:
    return common.allowed(user, action, SENIOR_ACTIONS)


def _base(return_id: str) -> str:
    return f"/post-order/v2/return/{return_id}"


# ------------------------------------------------------------------ 实时详情
def parse_detail(d: dict) -> dict:
    """getReturn（fieldgroups=FULL）里处理页要用的部分。"""
    s = d.get("summary") or d
    det = d.get("detail") or {}
    ci = s.get("creationInfo") or {}
    due = s.get("sellerResponseDue") or {}
    refund = det.get("refundInfo") or {}
    lines = []
    for x in (refund.get("estimatedRefundDetail") or {}).get("itemizedRefundDetails") or []:
        est = common.amount(x.get("estimatedAmount"))
        if est is None:
            continue
        lo, hi = common.amount(x.get("minAmount")), common.amount(x.get("maxAmount"))
        lines.append({"fee_type": x.get("refundFeeType") or "OTHER",
                      "label": FEE_LABEL.get(x.get("refundFeeType") or "", x.get("refundFeeType") or "其他"),
                      "estimated": est, "min": lo or est, "max": hi or est})
    actual = common.amount(((refund.get("actualRefundDetail") or {}).get("actualRefund") or {}).get("totalAmount")) \
        or common.amount((s.get("sellerTotalRefund") or {}).get("actualRefundAmount"))
    options = [o.get("actionType") for o in s.get("sellerAvailableOptions") or [] if o.get("actionType")]
    history = []
    for h in det.get("responseHistory") or []:
        attrs = h.get("attributes") or {}
        history.append({"actor": h.get("author"), "action": h.get("activity"), "at": common.dt(h.get("creationDate")),
                        "description": (h.get("notes") or "") if isinstance(h.get("notes"), str)
                        else ((h.get("notes") or {}).get("content") if isinstance(h.get("notes"), dict) else None),
                        "from_state": h.get("fromState"), "to_state": h.get("toState"),
                        "partial_refund": common.amount(attrs.get("partialRefundAmount")), "rma": attrs.get("RMA")})
    history.sort(key=lambda h: h["at"] or "")
    ship = det.get("returnShipmentInfo") or {}
    track = ship.get("shipmentTracking") or {}
    files = [{"id": f.get("fileId"), "name": f.get("fileName"), "purpose": f.get("filePurpose"),
              "status": f.get("fileStatus"), "by": f.get("submitter"), "at": common.dt(f.get("creationDate"))}
             for f in det.get("files") or []]
    total = round(sum(x["estimated"]["value"] for x in lines), 2) if lines else None
    cur = next((x["estimated"]["currency"] for x in lines if x["estimated"]["currency"]), None) \
        or (common.amount((s.get("sellerTotalRefund") or {}).get("estimatedRefundAmount")) or {}).get("currency")
    est_total = {"value": total, "currency": cur} if total is not None else \
        common.amount((s.get("sellerTotalRefund") or {}).get("estimatedRefundAmount"))
    return {
        "return_id": str(s.get("returnId") or ""), "order_id": s.get("orderId"), "state": s.get("state"), "status": s.get("status"),
        "type": s.get("currentType"), "reason": ci.get("reason"), "comment": (ci.get("comments") or {}).get("content"),
        "opened_at": common.dt(ci.get("creationDate")), "due_at": common.dt(due.get("respondByDate")),
        "seller_due": due.get("activityDue"), "timeout_at": common.dt(s.get("timeoutDate")),
        "rma_required": bool((s.get("returnPolicy") or {}).get("rmaRequired")),
        "options": options, "option_labels": [OPTION_LABEL.get(o, o) for o in options],
        "action_url": next((o.get("actionURL") for o in s.get("sellerAvailableOptions") or [] if o.get("actionURL")), None),
        "refund_lines": lines, "estimated_refund": est_total, "refunded": actual,
        "partial_refund_allowed": any(x["min"]["value"] < x["estimated"]["value"] - EPS for x in lines),
        "tracking": {"carrier": track.get("carrierName") or track.get("carrierEnum"), "number": track.get("trackingNumber"),
                     "status": track.get("deliveryStatus")} if track else None,
        "files": files, "history": history,
    }


def live_detail(store_id: str, return_id: str, api=None) -> dict:
    from .sources.ebay import EbayReadOnlyApi

    try:
        d = common.get_detail(store_id, _base(return_id), api or EbayReadOnlyApi(store_id), {"fieldgroups": "FULL"})
    except ActionError as e:
        if e.status_code == 404:
            raise ActionError(404, "eBay 上找不到这条退货") from None
        raise
    return parse_detail(d)


def can_do(live: dict | None) -> dict[str, bool]:
    """按 eBay 给卖家的下一步，各处理现在能不能做（没取到详情时只看本系统规则）。"""
    opts = set((live or {}).get("options") or [])
    out = {a: True for a in ACTIONS}
    if live is not None:
        for a, need in OPTION_FOR.items():
            out[a] = bool(opts & set(need))
    return out


# ------------------------------------------------------------------ 请求体
def _need_option(live: dict, action: str) -> None:
    if not can_do(live)[action]:
        now = "、".join(live.get("option_labels") or []) or "没有（可能在等买家）"
        raise ActionError(409, f"eBay 现在不允许{ACTIONS[action]}（eBay 给卖家的下一步：{now}）")


def refund_plan(live: dict, p: dict) -> tuple[str, dict, str]:
    """退款：(接口后缀, 请求体, 说明)。金额核对按 eBay 的估算和范围。"""
    if not p.get("confirm"):
        raise ActionError(422, "退款要先确认金额")
    opts = set(live.get("options") or [])
    lines, est = live.get("refund_lines") or [], live.get("estimated_refund")
    mode = p.get("mode") or "full"  # full 全额 / partial 部分（能直接退就退，只能提议时提议）/ offer 明确只提议
    if mode == "offer" and "SELLER_OFFER_PARTIAL_REFUND" not in opts:
        raise ActionError(409, "eBay 现在不允许向买家提出部分退款（eBay 给卖家的下一步："
                               + ("、".join(live.get("option_labels") or []) or "没有") + "）")
    try:
        want = round(float(p.get("amount")), 2)
    except (TypeError, ValueError):
        raise ActionError(422, "请填写退款金额") from None
    cur = (est or {}).get("currency")
    if p.get("currency") and cur and p["currency"] != cur:
        raise ActionError(409, f"币种和 eBay 上的不一致（eBay 是 {cur}），请刷新后再确认")
    note = common.text(p.get("comment"), "备注")
    comments = {"comments": {"content": note}} if note else {}
    if "SELLER_ISSUE_REFUND" in opts and mode != "offer":
        if not lines:
            raise ActionError(409, "eBay 没给出退款明细，不能在这里退款，请到 eBay 后台")
        items = [{"refundFeeType": x["fee_type"], "refundAmount": {"value": x["estimated"]["value"], "currency": cur}}
                 for x in lines]
        total = round(sum(x["estimated"]["value"] for x in lines), 2)
        if mode == "full":
            if abs(want - total) > EPS:
                raise ActionError(409, f"退款金额和 eBay 估算的全额（{cur} {total:.2f}）对不上，请刷新后再确认")
            label = f"全额退款 {cur} {total:.2f}"
        elif mode == "partial":
            pi = next((i for i, x in enumerate(lines) if x["fee_type"] == "PURCHASE_PRICE"), None)
            if pi is None or not live.get("partial_refund_allowed"):
                raise ActionError(409, "eBay 给的退款范围不能减少（只能全额退），请选全额退款")
            others = total - lines[pi]["estimated"]["value"]
            item_amt = round(want - others, 2)
            lo, hi = lines[pi]["min"]["value"], lines[pi]["max"]["value"]
            if item_amt < lo - EPS or item_amt > hi + EPS or want >= total - EPS:
                raise ActionError(422, f"部分退款金额要在 {cur} {lo + others:.2f}–{min(hi + others, total):.2f} 之间且少于全额"
                                       f"（eBay 允许商品价格这一项退 {lo:.2f}–{hi:.2f}）")
            items[pi]["refundAmount"]["value"] = item_amt
            label = f"部分退款 {cur} {want:.2f}（全额 {total:.2f}）"
        else:
            raise ActionError(422, "退款类型只能是全额或部分")
        return "issue_refund", {"refundDetail": {"itemizedRefundDetail": items,
                                                 "totalAmount": {"value": want, "currency": cur}}, **comments}, label
    if "SELLER_OFFER_PARTIAL_REFUND" in opts and mode in ("partial", "offer"):
        cap = (est or {}).get("value")
        if want <= 0 or (cap is not None and want >= cap - EPS):
            raise ActionError(422, f"部分退款金额要大于 0、少于 {cur or ''} {cap or 0:.2f}")
        keep = bool(p.get("keep_item", True))
        return "decide", {"decision": "OFFER_PARTIAL_REFUND", "partialRefundAmount": {"value": want, "currency": cur},
                          "keepOriginalItem": keep, **comments}, \
            f"已向买家提出部分退款 {cur} {want:.2f}，等买家同意{'（买家不用退回商品）' if keep else ''}"
    _need_option(live, "refund")
    raise ActionError(409, "eBay 现在只允许向买家提出部分退款（买家同意后才退），请选部分退款")


def _upload(store_id: str, return_id: str, data: str | None, name: str | None, purpose: str,
            http: httpx.Client | None) -> tuple[str | None, str]:
    raw, fname = common.file_data(data, name)
    r = common.post(store_id, f"{_base(return_id)}/file/upload", {"data": raw, "fileName": fname, "filePurpose": purpose}, http)
    return (r or {}).get("fileId"), fname


def accept_steps(case: EbayCase, return_id: str, live: dict, p: dict) -> list[tuple[str, dict | None, str]]:
    """接受退货要依次调的接口：[(接口后缀或 'upload', 请求体, 说明)]。先校验完全部参数再开始调。"""
    _need_option(live, "accept")
    opts = set(live.get("options") or [])
    method = p.get("method") or ""
    if method not in ACCEPT_METHODS:
        raise ActionError(422, "请选择处理方式")
    rma = common.text(p.get("rma"), "RMA 单号", max_len=64)
    note = common.text(p.get("comment"), "给平台的留言")
    comments = {"comments": {"content": note}} if note else {}
    steps: list[tuple[str, dict | None, str]] = []
    if "SELLER_APPROVE_REQUEST" in opts:
        if live.get("rma_required") and not rma:
            raise ActionError(422, "这个店铺开了 RMA，接受退货要填 RMA 单号")
        steps.append(("decide", {"decision": "APPROVE", **({"RMANumber": rma} if rma else {}), **comments},
                      "同意退货" + (f"，RMA {rma}" if rma else "")))
    elif rma and "SELLER_PROVIDE_RMA" in opts:
        steps.append(("decide", {"decision": "PROVIDE_RMA", "RMANumber": rma}, f"提供 RMA {rma}"))
    if method == "accept_only":
        if not steps:
            raise ActionError(409, "eBay 上这条退货已经同意过了，现在要提供退货标签（选其他处理方式）")
        return steps
    if "SELLER_PROVIDE_LABEL" not in opts and "SELLER_APPROVE_REQUEST" not in opts:
        raise ActionError(409, "eBay 现在不需要卖家提供退货标签")
    if method == "upload_label":
        number = common.text(p.get("tracking_number"), "运单号", True, 64)
        carrier = common.text(p.get("carrier"), "物流渠道", True, 64)
        common.file_data(p.get("file_data"), p.get("file_name"))  # 先校验文件
        steps.append(("upload", None, "上传退货面单"))
        steps.append(("add_shipping_label", {"labelAction": "UPLOAD_LABEL", "trackingNumber": number, "carrierName": carrier,
                                             **comments}, f"提供退货面单 {number}（{carrier}）"))
    elif method == "no_label":
        reason = p.get("no_label_reason")
        if reason not in NO_LABEL_REASONS:
            raise ActionError(422, "请选择不提供标签的原因")
        msg = common.text(p.get("comment"), "给平台的留言", True)
        steps.append(("add_shipping_label", {"labelAction": "NO_LABEL", "noLabelReason": reason, "comments": {"content": msg}},
                      f"不提供退货标签：{NO_LABEL_REASONS[reason]}"))
    else:  # ebay_label
        carrier = p.get("carrier_enum")
        if carrier not in EBAY_LABEL_CARRIERS:
            raise ActionError(422, "请选择 eBay 运输标签的承运商")
        steps.append(("add_shipping_label", {"labelAction": "EBAY_LABEL", "carrierEnum": carrier, **comments},
                      f"使用 eBay 运输标签（{carrier}）"))
    return steps


# ------------------------------------------------------------------ 执行
def run(db: Session, case: EbayCase, return_id: str, action: str, payload: dict, user, *,
        http: httpx.Client | None = None, api=None) -> dict:
    """在 eBay 上处理一条退货（case 是记处理记录的售后单：退货本身，或退货升级来的纠纷）。
    返回 {done, live, partial_error?}；发信返回 {done, queued}。"""
    base = _base(return_id)
    if action == "refund":
        raise ActionError(409, REFUND_VIA_REQUEST)
    if action == "message":
        return {"done": "已放进发件箱", "live": None,
                "queued": common.queue_message(db, case, f"{base}/send_message", payload.get("text"), user,
                                               text_zh=payload.get("text_zh"), handling_ms=payload.get("handling_ms"))}
    live = live_detail(case.store_id, return_id, api)  # 每次处理前实时取：按 eBay 现在允许的做
    done: list[str] = []
    partial_error = None
    if action == "file":
        purpose = payload.get("purpose") or "ITEM_RELATED"
        if purpose not in FILE_PURPOSES:
            raise ActionError(422, "请选择文件用途")
        _, fname = _upload(case.store_id, return_id, payload.get("file_data"), payload.get("file_name"), purpose, http)
        done.append(f"上传文件 {fname}（{FILE_PURPOSES[purpose]}）")
    elif action == "received":
        _need_option(live, "received")
        note = common.text(payload.get("comment"), "备注")
        common.post(case.store_id, f"{base}/mark_as_received", {"comments": {"content": note}} if note else {}, http)
        done.append("确认已收到退货")
    elif action == "accept":
        steps = accept_steps(case, return_id, live, payload)
        file_id = None
        for suffix, body, label in steps:
            try:
                if suffix == "upload":
                    file_id, _ = _upload(case.store_id, return_id, payload.get("file_data"), payload.get("file_name"),
                                         "LABEL_RELATED", http)
                    continue
                if suffix == "add_shipping_label" and file_id:
                    body = {**body, "fileId": file_id}
                common.post(case.store_id, f"{base}/{suffix}", body, http)
                done.append(label)
            except ActionError as e:
                if not done:
                    raise
                partial_error = f"{label}没有完成：{e.message}；前面的“{'、'.join(done)}”已经在 eBay 上做了，请到 eBay 后台补上"
                break
    else:
        raise ActionError(422, "没有这种处理")
    summary = "，".join(done)
    common.record_action(db, case, action=action, label=ACTIONS[action], summary=summary, user=user)
    live = _refresh(case, return_id, api)
    db.flush()
    return {"done": summary, "live": live, **({"partial_error": partial_error} if partial_error else {})}


def _refresh(case: EbayCase, return_id: str, api=None) -> dict | None:
    """处理后刷新 eBay 上的退货状态（失败不影响已经做成的处理）。"""
    try:
        live = live_detail(case.store_id, return_id, api)
    except Exception as e:  # noqa: BLE001
        log.warning("处理后刷新退货 %s 失败：%s", return_id, e)
        return None
    if case.kind == "RETURN" and (live.get("state") or live.get("status")):
        from .ebay_cases import _closed

        case.status = f"{live.get('state')}/{live.get('status')}"[:64]
        case.is_open = not _closed(live.get("state"), live.get("status"))
    return live


def return_id_for(case: EbayCase, api=None) -> str:
    """退款申请单要退的那条退货：退货本身，或纠纷对应的退货（getCase 的 returnId）。"""
    if case.kind == "RETURN":
        return case.external_id
    if case.kind == "CASE":
        from .case_actions import live_detail as case_detail

        rid = case_detail(case.store_id, case.external_id, api).get("return_id")
        if rid:
            return rid
    raise ActionError(409, "这条售后记录没有对应的退货，不能按退货退款")


def execute_refund_request(db: Session, rr, user, *, http: httpx.Client | None = None, api=None) -> dict:
    """退款申请单（execution=post_order_return）审核通过或命中“直接退款”时由 2 号的 refund_requests 调用：
    用退货的 Post-Order 接口退款。rr 用到：id、case_id、return_id、return_payload {mode: full|partial, keep_item}、
    full、amount、currency、comment。

    核对放在执行时：实时取退货（sellerAvailableOptions、每一项的估算和 min / max）、这条退货是否退过、订单是否已全额退款。
    调 eBay 之前挡下的一律抛 403 / 404 / 409（called_ebay=False，申请单留在待审核）；调 eBay 失败抛 422（eBay 拒绝）/
    502（超时 / 5xx，不确定，不重试），called_ebay=True，申请单记“处理异常”。
    成功：记售后单处理（record_action：审计、处理备注、会话系统事件、会话售后状态 / 备注）、刷新退货状态，返回
    {"state": "refunded" 已退 / "pending" eBay 处理中 / "offered" 已向买家提出部分退款（等买家同意），"summary",
     "return_id", "refund_id": None（Post-Order 不返回）, "refund_status": eBay 返回的状态}。申请单状态由调用方写。"""
    from .models import EbayCase as _Case

    try:
        case = db.get(_Case, rr.case_id) if getattr(rr, "case_id", None) else None
        if case is None or case.kind not in ("RETURN", "CASE"):
            raise ActionError(409, "退款申请单没有关联退货或退货纠纷，不能按退货退款")
        why = common.cannot_act(db.get(common.Store, case.store_id))
        if why:
            raise ActionError(403, why)
        opts = dict(getattr(rr, "return_payload", None) or {})
        return_id = str(getattr(rr, "return_id", None) or opts.get("return_id") or return_id_for(case, api))
        live = live_detail(case.store_id, return_id, api)
        if live.get("refunded") and live["refunded"]["value"] > 0:
            raise ActionError(409, f"eBay 显示这条退货已经退过款（{live['refunded']['currency'] or ''} "
                                   f"{live['refunded']['value']:.2f}），不能再退")
        mode = opts.get("mode") or ("full" if rr.full else "partial")
        payload = {"mode": mode, "amount": rr.amount, "currency": rr.currency, "confirm": True,
                   "comment": getattr(rr, "comment", None), "keep_item": bool(opts.get("keep_item", True))}
        suffix, body, label = refund_plan(live, payload)  # 按 eBay 现在允许的和金额范围核对
        common.ensure_not_refunded(case.store_id, live.get("order_id") or (case.order_id if case.kind == "RETURN" else None))
    except ActionError as e:
        if e.called_ebay or e.status_code in (403, 404, 409):
            raise
        raise ActionError(409, e.message) from None  # 执行前的核对没过：申请单留在待审核
    path = f"{_base(return_id)}/{suffix}"
    res = common.post(case.store_id, path, body, http) or {}
    offered = suffix == "decide"
    status = str(res.get("refundStatus") or "").upper() or None
    if status == "FAILED":
        err = ActionError(422, "eBay 返回退款失败（refundStatus=FAILED），请到 eBay 后台查看原因")
        err.called_ebay, err.endpoint = True, path
        raise err
    state = "offered" if offered else ("refunded" if status in ("SUCCESS", "REFUNDED", "COMPLETED") else "pending")
    summary = f"{label}（退款申请 #{rr.id}）"
    common.record_action(db, case, action="refund", label="退款", summary=summary, user=user,
                         work_state="PROCESSING" if offered else "DONE", aftersales_done=not offered)
    _refresh(case, return_id, api)
    db.flush()
    return {"state": state, "summary": summary, "return_id": return_id, "refund_id": None, "refund_status": status}


def perform(db: Session, case: EbayCase, action: str, payload: dict, user, *, http: httpx.Client | None = None,
            api=None) -> dict:
    """退货处理页的入口。"""
    common.check(db, case, "RETURN", action, ACTIONS, SENIOR_ACTIONS, user, open_required=action != "message")
    return run(db, case, case.external_id, action, payload, user, http=http, api=api)
