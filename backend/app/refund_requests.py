"""退款申请单（替代 ERP 的“退款单”审核流）。

流程：客服 / 售后提交（会话侧栏订单、退货 / 纠纷 / 咨询 / 取消页）→ 按审核规则（refund_review_rules：店铺 + 币种 + 金额范围）
决定“先审核”还是“直接退款”（默认先审核）→ 管理员 / 售后审核通过 → 调 eBay Fulfillment issueRefund
（POST apiz /sell/fulfillment/v1/order/{orderId}/issue_refund，令牌权限 sell.finances，EU/UK 卖家要数字签名）。
eBay 的退款是异步的：接受后返回 refundId + PENDING，状态记为“退款中”；之后拉到这笔订单的退款流水（Finances）
或实时查订单看到这笔 refundId 已退，就记为“已退款”（reconcile / refresh_live）。

约束（和咨询处理一致）：
- 只对对接 eBay 真实收发的店铺开放（EBAY_LIVE_STORES，EBAY_CHANNEL=ebay），其他店铺提示到 eBay 后台；
- 提交：能看这个店铺的账号都可以；审核、直接退款：只有管理员和售后；
- 金额：前端把确认的金额、币种原样带回，和申请单不一致就不提交；调 eBay 前实时查订单，超过“订单总额 − 已退金额”不退；
  同一订单有没处理完的申请（待审核 / 退款中 / 处理异常）不能再提交；
- 超时 / 5xx 不重试（不确定 eBay 有没有收到），记“处理异常”，由人到 eBay 后台核对后标“已退款”或“没退，退回待审核”；
- 成功后：写会话事件（AI 起草能看到）、会话售后备注、关联售后记录的处理备注、审计，后台重算这家店的售后原因。
  给买家的通知不自动发，由客服在会话里用 AI 起草后人工审核发送。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import case_actions_common as cac
from .case_actions_common import ActionError as RefundError
from .config import get_settings
from .db import utcnow
from .models import (AuditEvent, Conversation, ConversationEvent, EbayCase, EbayOrder, EbayOrderLine, RefundRequest,
                     RefundReviewRule, Store)

log = logging.getLogger(__name__)

R = RefundRequest
REASONS = {
    "BUYER_CANCEL": "买家取消订单", "SELLER_CANCEL": "卖家取消（缺货等）", "ITEM_NOT_RECEIVED": "买家没收到货",
    "BUYER_RETURN": "买家退货（非质量问题）", "ITEM_NOT_AS_DESCRIBED": "与描述不符 / 质量问题", "OTHER_ADJUSTMENT": "其他调整（如部分补偿）",
    "SHIPPING_DISCOUNT": "退运费差价",
}
SOURCES = {"MANUAL": "主动退款", "AFTERSALES": "纠纷退货", "CANCEL": "取消交易"}
ORDER, RETURN = "order_issue_refund", "post_order_return"
EXECUTIONS = {ORDER: "按订单退款（Fulfillment issueRefund）", RETURN: "通过退货退款（Post-Order）"}
RETURN_STATE = {"refunded": R.REFUNDED, "pending": R.REFUNDING, "offered": R.REFUNDING}
OPEN_STATUSES = (R.PENDING, R.REFUNDING, R.ERROR)  # 没处理完的：同一订单不能再提交
REFUND_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.finances"  # issueRefund 要的权限
MAX_COMMENT = 1000
EPS = 0.005
BACKGROUND = True  # 成功后在后台线程重算售后原因


# ------------------------------------------------------------------ 权限、规则
def is_senior(user) -> bool:
    """能审核、直接退款的角色：管理员和售后。"""
    return user is not None and user.role in cac.SENIOR_ROLES


cannot_act = cac.cannot_act  # 不能在本系统退款的原因；None = 可以


def rules(db: Session) -> list[RefundReviewRule]:
    return list(db.scalars(select(RefundReviewRule).order_by(RefundReviewRule.sort, RefundReviewRule.id)))


def decide_mode(db: Session, store_id: str, amount: float, currency: str) -> tuple[str, RefundReviewRule | None]:
    """按规则决定 direct 直接退款 / review 先审核：按 sort 第一条命中的生效；都没命中 = 先审核。
    店铺专属的规则先于“全部店铺”的规则（同 sort 时）。"""
    cands = [r for r in rules(db) if r.enabled]
    cands.sort(key=lambda r: (r.sort, 0 if r.store_id else 1, r.id))
    for r in cands:
        if r.store_id and r.store_id != store_id:
            continue
        if r.currency and r.currency.upper() != (currency or "").upper():
            continue
        if r.min_amount is not None and amount < r.min_amount - EPS:
            continue
        if r.max_amount is not None and amount > r.max_amount + EPS:
            continue
        return (r.mode if r.mode in ("direct", "review") else "review"), r
    return "review", None


def rule_dict(r: RefundReviewRule) -> dict:
    return {"id": r.id, "store_id": r.store_id, "currency": r.currency, "min_amount": r.min_amount, "max_amount": r.max_amount,
            "mode": r.mode, "enabled": bool(r.enabled), "sort": r.sort, "note": r.note}


def save_rules(db: Session, items: list[dict], actor: str) -> list[RefundReviewRule]:
    """整表替换审核规则（管理员）。"""
    clean = []
    for i, it in enumerate(items):
        mode = it.get("mode")
        if mode not in ("direct", "review"):
            raise RefundError(422, f"第 {i + 1} 条：处理方式只能是“直接退款”或“先审核”")
        lo, hi = it.get("min_amount"), it.get("max_amount")
        for v in (lo, hi):
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                raise RefundError(422, f"第 {i + 1} 条：金额要是不小于 0 的数字")
        if lo is not None and hi is not None and lo > hi:
            raise RefundError(422, f"第 {i + 1} 条：金额下限比上限大")
        sid = (it.get("store_id") or None)
        if sid and db.get(Store, sid) is None:
            raise RefundError(422, f"第 {i + 1} 条：店铺不存在")
        cur = (it.get("currency") or "").strip().upper() or None
        if cur and len(cur) != 3:
            raise RefundError(422, f"第 {i + 1} 条：币种写 3 个字母，如 EUR")
        clean.append(RefundReviewRule(store_id=sid, currency=cur, min_amount=lo, max_amount=hi, mode=mode,
                                      enabled=bool(it.get("enabled", True)), sort=i, note=(it.get("note") or "")[:200] or None,
                                      created_by=actor[:64], created_at=utcnow(), updated_at=utcnow()))
    before = [rule_dict(r) for r in rules(db)]
    for r in rules(db):
        db.delete(r)
    db.flush()
    for r in clean:
        db.add(r)
    db.add(AuditEvent(actor=actor[:64], action="REFUND_RULES_SAVED", detail={"before": before, "count": len(clean)}))
    db.flush()
    return clean


# ------------------------------------------------------------------ 订单信息（读库）
def _order(db: Session, store_id: str, order_id: str) -> EbayOrder | None:
    return db.scalar(select(EbayOrder).where(EbayOrder.store_id == store_id, EbayOrder.order_id == order_id))


def _lines(db: Session, store_id: str, order_id: str) -> list[EbayOrderLine]:
    return list(db.scalars(select(EbayOrderLine).where(EbayOrderLine.store_id == store_id, EbayOrderLine.order_id == order_id)
                           .order_by(EbayOrderLine.id)))


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def requested_total(db: Session, store_id: str, order_id: str, exclude_id: int | None = None) -> float:
    """这张订单在本系统已退 / 正在退 / 待审核的金额合计（不含审核不通过的）。"""
    q = select(func.coalesce(func.sum(R.amount), 0.0)).where(R.store_id == store_id, R.order_id == order_id,
                                                             R.status.in_((R.PENDING, R.REFUNDING, R.REFUNDED, R.ERROR)))
    if exclude_id:
        q = q.where(R.id != exclude_id)
    return float(db.scalar(q) or 0)


OPEN_KIND_LABEL = {"RETURN": "退货", "CASE": "纠纷", "INQUIRY": "未收到货咨询", "CANCEL": "取消申请"}


def open_case_block(db: Session, store_id: str, order_id: str | None, *, return_id: str | None = None,
                    allow_case_id: int | None = None, via_return: bool = False) -> str | None:
    """这张订单在 eBay 上还有进行中的退货 / 纠纷 / 咨询 / 取消申请时不能用订单退款（Fulfillment issueRefund）：
    那样 eBay 上的退货等不会推进关闭，买家寄回后还可能再退一次；取消申请同意后 eBay 会自动退款。返回提示，None = 可以。
    通过退货退款（post_order_return）时，这条退货本身（return_id）和发起它的那条记录（allow_case_id，如退货升级来的纠纷）
    不算阻碍——退的就是它；订单上别的开着的记录照样拦。"""
    if not order_id:
        return None
    keys = {order_id, *db.scalars(select(EbayOrderLine.line_key).where(
        EbayOrderLine.store_id == store_id, EbayOrderLine.order_id == order_id, EbayOrderLine.line_key.is_not(None)))}
    # 通过退货退（产品经理 11-12 / 11-15 定）：退的就是退货本身，重复退款由 return_actions 实时核对（这条退货有没有退过、
    # 订单是否已全额退款、eBay 现在允不允许退）；只有同一订单进行中的取消申请要拦（同意取消后 eBay 自动退款），
    # 别的退货 / 纠纷 / 咨询只在弹窗里提示（open_case_notice）
    kinds = ("CANCEL",) if via_return else tuple(OPEN_KIND_LABEL)
    q = select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.order_id.in_(keys), EbayCase.is_open.is_(True),
                               EbayCase.kind.in_(kinds), EbayCase.hidden_at.is_(None))
    if return_id:
        q = q.where(~((EbayCase.kind == "RETURN") & (EbayCase.external_id == return_id)))
    if allow_case_id:
        q = q.where(EbayCase.id != allow_case_id)
    c = db.scalar(q.order_by(EbayCase.opened_at.desc()).limit(1))
    if c is None:
        return None
    k = OPEN_KIND_LABEL[c.kind]
    if c.kind == "CANCEL" and via_return:
        return f"这张订单有进行中的{k}（{c.external_id}）：请先到“取消交易”里同意或拒绝（同意后 eBay 会自动退款），再决定要不要通过退货退款"
    if c.kind == "CANCEL":
        return f"这张订单有进行中的{k}（{c.external_id}）：请到“取消交易”里同意或拒绝，同意后 eBay 会自动退款，不要再另外退款"
    return f"这张订单有进行中的{k}（{c.external_id}）：请在“{k}”的纠纷处理页里退款（eBay 才会推进关闭这条{k}，避免重复退款）"


def open_case_notice(db: Session, store_id: str, order_id: str | None, *, return_id: str | None = None,
                     allow_case_id: int | None = None) -> str | None:
    """通过退货退款时，同一订单上别的进行中的退货 / 纠纷 / 咨询：不拦，只在弹窗里提示（避免两边都退）。"""
    if not order_id:
        return None
    keys = {order_id, *db.scalars(select(EbayOrderLine.line_key).where(
        EbayOrderLine.store_id == store_id, EbayOrderLine.order_id == order_id, EbayOrderLine.line_key.is_not(None)))}
    q = select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.order_id.in_(keys), EbayCase.is_open.is_(True),
                               EbayCase.kind.in_(("RETURN", "CASE", "INQUIRY")), EbayCase.hidden_at.is_(None))
    if return_id:
        q = q.where(~((EbayCase.kind == "RETURN") & (EbayCase.external_id == return_id)))
    if allow_case_id:
        q = q.where(EbayCase.id != allow_case_id)
    others = list(db.scalars(q.order_by(EbayCase.opened_at.desc()).limit(5)))
    if not others:
        return None
    names = "、".join(f"{OPEN_KIND_LABEL[c.kind]} {c.external_id}" for c in others)
    return f"这张订单上还有进行中的 {names}：确认这次退款和它们不是同一笔，避免重复退款"


def _default_reason(case: EbayCase | None, source: str) -> str:
    if source == "CANCEL":
        return "BUYER_CANCEL"
    if case is None:
        return "OTHER_ADJUSTMENT"
    if case.kind in ("INQUIRY",) or (case.kind == "CASE" and (case.reason or "").upper().find("NOT_RECEIVED") >= 0):
        return "ITEM_NOT_RECEIVED"
    if case.kind == "RETURN":
        nad = {"NOT_AS_DESCRIBED", "SIGNIFICANTLY_NOT_AS_DESCRIBED", "DEFECTIVE_ITEM", "ARRIVED_DAMAGED", "MISSING_PARTS",
               "DOES_NOT_FIT", "WRONG_SIZE", "ORDERED_DIFFERENT_ITEM"}
        return "ITEM_NOT_AS_DESCRIBED" if (case.reason or "") in nad else "BUYER_RETURN"
    return "OTHER_ADJUSTMENT"


def _source_of(case: EbayCase | None) -> str:
    if case is None:
        return "MANUAL"
    return "CANCEL" if case.kind == "CANCEL" else "AFTERSALES"


def _find_conversation(db: Session, store_id: str, buyer: str | None) -> int | None:
    """这个买家在这家店最近有消息的会话（和 case_actions_common.conversation_for 同一规则）。"""
    if not buyer:
        return None
    return db.scalar(select(Conversation.id).where(Conversation.store_id == store_id,
                                                   func.lower(Conversation.buyer_key) == buyer.lower(),
                                                   Conversation.conversation_type != "FROM_EBAY")
                     .order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(1))


def prefill(db: Session, user, *, case_id: int | None = None, conversation_id: int | None = None,
            order_id: str | None = None, store_id: str | None = None) -> dict:
    """提交表单的预填：店铺、订单（读库）、订单行、已申请金额、建议的来源和原因、审核方式、能不能在系统里退。"""
    from .auth import check_store

    case = None
    if case_id:
        case = db.get(EbayCase, case_id)
        if case is None:
            raise RefundError(404, "售后记录不存在")
        check_store(case.store_id)
        store_id = case.store_id
        order_id = order_id or case.order_id
        if case.kind in ("CASE", "INQUIRY") and order_id:  # 纠纷、咨询的订单号是 商品号-交易号：换成订单号
            ln = db.scalar(select(EbayOrderLine).where(EbayOrderLine.store_id == case.store_id, EbayOrderLine.line_key == order_id))
            order_id = ln.order_id if ln else order_id
    conv = None
    if conversation_id:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            raise RefundError(404, "会话不存在")
        check_store(conv.store_id)
        store_id = store_id or conv.store_id
    if not store_id:
        raise RefundError(422, "请指定店铺")
    check_store(store_id)
    store = db.get(Store, store_id)
    if store is None:
        raise RefundError(404, "店铺不存在")
    orders = []
    if conv is not None and not order_id:  # 会话里没指定订单：列出这个买家在这家店的订单让人选
        orders = [o.order_id for o in db.scalars(select(EbayOrder).where(
            EbayOrder.store_id == store_id, func.lower(EbayOrder.buyer) == (conv.buyer_key or "").lower())
            .order_by(EbayOrder.created_at.desc()).limit(20))]
        order_id = orders[0] if len(orders) == 1 else None
    o = _order(db, store_id, order_id) if order_id else None
    lines = _lines(db, store_id, order_id) if order_id else []
    source = _source_of(case)
    total = o.total if o else None
    cur = (o.currency if o else None) or (lines[0].price_currency if lines else None)
    already = requested_total(db, store_id, order_id) if order_id else 0.0
    open_req = db.scalar(select(R).where(R.store_id == store_id, R.order_id == order_id, R.status.in_(OPEN_STATUSES))
                         .order_by(R.id.desc()).limit(1)) if order_id else None
    remaining = round(max(0.0, (total or 0) - already), 2) if total is not None else None
    mode, rule = decide_mode(db, store_id, remaining or 0, cur or "") if remaining else ("review", None)
    via = case is not None and case.kind == "RETURN" and case.is_open
    execution, return_id = (RETURN, case.external_id) if via else (ORDER, None)
    blocked = cannot_act(store) or open_case_block(db, store_id, order_id, return_id=return_id,
                                                   allow_case_id=case.id if via else None, via_return=via)
    notice = open_case_notice(db, store_id, order_id, return_id=return_id, allow_case_id=case.id) if via else None
    return {
        "execution": execution, "execution_label": EXECUTIONS[execution], "return_id": return_id,
        "open_case_notice": notice,
        "store_id": store_id, "store_name": store.name, "order_id": order_id, "orders": orders,
        "conversation_id": conv.id if conv else (_find_conversation(db, store_id, o.buyer if o else None) if o else None),
        "buyer": (o.buyer if o else None) or (case.buyer if case else None) or (conv.buyer_key if conv else None),
        "case": ({"id": case.id, "kind": case.kind, "external_id": case.external_id, "reason": case.reason,
                  "status": case.status, "amount": case.amount, "currency": case.currency} if case else None),
        "source": source, "source_label": SOURCES[source], "reason": _default_reason(case, source),
        "order": ({"total": total, "currency": cur, "created_at": o.created_at.isoformat() + "Z" if o and o.created_at else None,
                   "refunded_flag": bool(o.refunded), "cancelled": bool(o.cancelled)} if o else None),
        "lines": [{"line_item_id": ln.line_item_id, "sku": ln.sku, "title": ln.title, "quantity": ln.quantity,
                   "amount": _num(ln.price_value), "currency": ln.price_currency or cur} for ln in lines],
        "requested": round(already, 2), "remaining": remaining,
        "open_request": to_dict(open_req) if open_req else None,
        "mode": mode, "rule": rule_dict(rule) if rule else None,
        "blocked": blocked, "can_submit": not blocked and bool(order_id) and open_req is None,
        "can_refund_directly": not blocked and is_senior(user),
        "reasons": [{"code": k, "label": v} for k, v in REASONS.items()],
    }


# ------------------------------------------------------------------ 提交 / 审核
def _text(v, field: str, limit: int = MAX_COMMENT) -> str | None:
    return cac.text(v, field, False, limit)


def _check_confirm(amount: float, currency: str, confirm_amount, confirm_currency) -> None:
    if confirm_amount is None or abs(float(confirm_amount) - float(amount)) > EPS \
            or (confirm_currency or "").upper() != (currency or "").upper():
        raise RefundError(409, "确认的金额和申请单对不上，请刷新后重新确认")


def _local_return_id(db: Session, store_id: str, order_id: str | None) -> str | None:
    """这张订单在本地的退货编号（开着的优先）。"""
    if not order_id:
        return None
    return db.scalar(select(EbayCase.external_id).where(EbayCase.store_id == store_id, EbayCase.kind == "RETURN",
                                                        EbayCase.order_id == order_id)
                     .order_by(EbayCase.is_open.desc(), EbayCase.opened_at.desc()).limit(1))


def _execution_of(db: Session, case: EbayCase | None, data: dict, store_id: str, order_id: str) -> tuple[str, str | None]:
    """执行方式：开着的退货、退货升级来的纠纷默认通过退货退；其余按订单退。前端也可以明确指定。
    退货编号没给时：退货取它自己，纠纷先按订单在本地找退货，找不到留空，由 return_actions.execute_refund_request 按 getCase 推。"""
    want = data.get("execution")
    if want and want not in EXECUTIONS:
        raise RefundError(422, "执行方式不对")
    if want == ORDER:
        return ORDER, None
    rid = (str(data.get("return_id") or "").strip() or None)
    if case is not None and case.kind == "RETURN":
        rid = rid or case.external_id
    if want == RETURN or rid:
        if case is not None and case.kind == "CASE" and not rid:  # 退货升级来的纠纷：本地记录 → 本地退货 → 实时 getCase
            rid = (case.detail or {}).get("return_id") or _local_return_id(db, store_id, order_id)
            if not rid:
                from . import return_actions

                finder = getattr(return_actions, "return_id_for", None)
                rid = finder(case) if finder else None  # 不是退货升级来的：抛 ActionError 409，原样返回
        elif not rid:
            raise RefundError(422, "通过退货退款要指定退货编号或从退货 / 纠纷发起")
        return RETURN, (rid or None) and rid[:64]
    return ORDER, None


def create(db: Session, user, data: dict, *, http: httpx.Client | None = None) -> RefundRequest:
    """提交退款申请。命中“直接退款”规则且提交人是管理员 / 售后时直接调 eBay 退款。"""
    from .auth import check_store

    case = db.get(EbayCase, data["case_id"]) if data.get("case_id") else None
    if data.get("case_id") and case is None:
        raise RefundError(404, "售后记录不存在")
    store_id = data.get("store_id") or (case.store_id if case else None)
    conv = db.get(Conversation, data["conversation_id"]) if data.get("conversation_id") else None
    if data.get("conversation_id") and conv is None:
        raise RefundError(404, "会话不存在")
    store_id = store_id or (conv.store_id if conv else None)
    if not store_id:
        raise RefundError(422, "请指定店铺")
    for sid in {store_id, case.store_id if case else store_id, conv.store_id if conv else store_id}:
        check_store(sid)
    if (case and case.store_id != store_id) or (conv and conv.store_id != store_id):
        raise RefundError(422, "店铺和售后记录 / 会话对不上")
    blocked = cannot_act(db.get(Store, store_id))
    if blocked:  # 不能在本系统退的店铺：不收申请（收了也没人能执行），提示到 eBay 后台
        raise RefundError(403, blocked)
    order_id = (data.get("order_id") or "").strip()
    if not order_id:
        raise RefundError(422, "请选择订单")
    o = _order(db, store_id, order_id)
    if o is None:
        raise RefundError(404, "本系统里没有这张订单（订单每小时同步一次，刚下的单稍后再试）")
    source = data.get("source") or _source_of(case)
    if source not in SOURCES:
        raise RefundError(422, "来源不对")
    reason = data.get("reason") or _default_reason(case, source)
    if reason not in REASONS:
        raise RefundError(422, "请选择退款原因")
    currency = (data.get("currency") or o.currency or "").upper()
    if o.currency and currency != o.currency.upper():
        raise RefundError(422, f"币种要和订单一致（{o.currency}）")
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        raise RefundError(422, "请填写退款金额") from None
    if amount <= 0:
        raise RefundError(422, "退款金额要大于 0")
    _check_confirm(amount, currency, data.get("confirm_amount"), data.get("confirm_currency"))
    execution, return_id = _execution_of(db, case, data, store_id, order_id)
    payload = data.get("return_payload") if execution == RETURN else None
    if payload is not None and (not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False)) > 8000):
        raise RefundError(422, "退货退款的参数格式不对")
    open_case = open_case_block(db, store_id, order_id, return_id=return_id if execution == RETURN else None,
                                allow_case_id=case.id if (case and execution == RETURN) else None, via_return=execution == RETURN)
    if open_case:
        raise RefundError(409, open_case)
    if db.scalar(select(R.id).where(R.store_id == store_id, R.order_id == order_id, R.status.in_(OPEN_STATUSES))):
        raise RefundError(409, "这张订单已经有没处理完的退款申请（待审核 / 退款中 / 处理异常），请先处理那一张")
    already = requested_total(db, store_id, order_id)
    if o.total is not None and amount > o.total - already + EPS:
        raise RefundError(409, f"退款金额超过可退金额（订单 {o.total:.2f} {o.currency}，本系统已申请 {already:.2f}）")
    full = bool(data.get("full"))
    if execution == ORDER and full and o.total is not None and (already > EPS or abs(amount - o.total) > EPS):
        raise RefundError(422, "全额退款的金额要等于订单总额，且这张订单之前没退过；否则请选部分退款")
    lines = None
    if data.get("lines") and execution == ORDER:
        known = {ln.line_item_id: ln for ln in _lines(db, store_id, order_id)}
        lines = []
        for it in data["lines"]:
            ln = known.get(str(it.get("line_item_id") or ""))
            if ln is None:
                raise RefundError(422, "订单行不属于这张订单")
            a = _num(it.get("amount"))
            if a is None or a <= 0:
                raise RefundError(422, "每个订单行的退款金额要大于 0")
            lines.append({"line_item_id": ln.line_item_id, "amount": round(a, 2), "sku": ln.sku, "title": (ln.title or "")[:120]})
        if abs(sum(x["amount"] for x in lines) - amount) > EPS:
            raise RefundError(422, "各订单行金额加起来要等于退款金额")
    mode, rule = decide_mode(db, store_id, amount, currency)
    who = (user.display_name if user else "system")[:64]
    rr = R(store_id=store_id, order_id=order_id, buyer=o.buyer, source=source,
           conversation_id=conv.id if conv else _find_conversation(db, store_id, o.buyer),
           case_id=case.id if case else None, case_kind=case.kind if case else None,
           case_external_id=case.external_id if case else None, full=full, amount=amount, currency=currency, lines=lines,
           reason=reason, comment=_text(data.get("comment"), "给买家的说明"), note=_text(data.get("note"), "内部备注", 2000),
           execution=execution, return_id=return_id, return_payload=payload,
           order_total=o.total, order_currency=o.currency, review_mode=mode, rule_id=rule.id if rule else None,
           status=R.PENDING, submitted_by=who, submitted_at=utcnow(), updated_at=utcnow())
    db.add(rr)
    db.flush()
    db.add(AuditEvent(actor=who, action="REFUND_REQUEST_CREATED", store_id=store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "order_id": order_id, "amount": amount, "currency": currency, "source": source,
                              "reason": reason, "mode": mode, "execution": execution, "return_id": return_id,
                              "rule": rule_dict(rule) if rule else None}))  # 命中的规则：ID、店铺、币种、金额范围
    if mode == "direct" and is_senior(user) and not cannot_act(db.get(Store, store_id)):
        rr.reviewed_by, rr.reviewed_at, rr.review_note = who, utcnow(), f"审核规则 #{rule.id if rule else '-'}：直接退款"
        db.add(AuditEvent(actor=who, action="REFUND_DIRECT_BY_RULE", store_id=store_id, conversation_id=rr.conversation_id,
                          detail={"id": rr.id, "amount": amount, "currency": currency, "rule": rule_dict(rule) if rule else None}))
        try:
            execute(db, rr, user, http=http)
        except RefundError as e:  # 退之前的核对没过（如 eBay 上已退过）：申请单留着，转人工审核
            rr.reviewed_by = rr.reviewed_at = None
            rr.review_note, rr.error = None, f"直接退款没有执行：{e.message}"
    db.flush()
    return rr


def _load_for_review(db: Session, rr: RefundRequest, user) -> None:
    from .auth import check_store

    check_store(rr.store_id)
    if not is_senior(user):
        raise RefundError(403, "退款审核只有管理员和售后可以做")


def approve(db: Session, rr: RefundRequest, user, data: dict, *, http: httpx.Client | None = None) -> RefundRequest:
    _load_for_review(db, rr, user)
    if rr.status != R.PENDING:
        raise RefundError(409, f"这张申请现在是“{R.STATUS_LABELS.get(rr.status, rr.status)}”，不能审核")
    _check_confirm(rr.amount, rr.currency, data.get("confirm_amount"), data.get("confirm_currency"))
    who = user.display_name[:64]
    rr.reviewed_by, rr.reviewed_at = who, utcnow()
    rr.review_note = _text(data.get("note"), "审核备注", 2000) or rr.review_note
    db.add(AuditEvent(actor=who, action="REFUND_REQUEST_APPROVED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "amount": rr.amount, "currency": rr.currency}))
    execute(db, rr, user, http=http)
    return rr


def reject(db: Session, rr: RefundRequest, user, note: str | None) -> RefundRequest:
    _load_for_review(db, rr, user)
    if rr.status != R.PENDING:
        raise RefundError(409, f"这张申请现在是“{R.STATUS_LABELS.get(rr.status, rr.status)}”，不能审核")
    note = _text(note, "不通过原因", 2000)
    if not note:
        raise RefundError(422, "请填写不通过的原因")
    who = user.display_name[:64]
    rr.status, rr.reviewed_by, rr.reviewed_at, rr.review_note, rr.updated_at = R.REJECTED, who, utcnow(), note, utcnow()
    db.add(AuditEvent(actor=who, action="REFUND_REQUEST_REJECTED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "note": note}))
    db.flush()
    return rr


def resolve(db: Session, rr: RefundRequest, user, outcome: str, refund_id: str | None = None, note: str | None = None) -> RefundRequest:
    """处理异常的单子：人到 eBay 后台核对后，标“已退款”（可填退款 ID）或“没退，退回待审核”。"""
    _load_for_review(db, rr, user)
    if rr.status != R.ERROR:
        raise RefundError(409, "只有“处理异常”的申请需要核对")
    who = user.display_name[:64]
    note = _text(note, "核对说明", 2000)
    if outcome == "refunded":
        rr.status = R.REFUNDED
        rr.ebay_refund_id = (refund_id or "").strip()[:64] or rr.ebay_refund_id
        _after_refund(db, rr, user, checked_by_hand=True)
    elif outcome == "not_refunded":
        rr.status, rr.error = R.PENDING, f"{rr.error or ''}（{who} 核对：eBay 上没有退款，退回待审核）".strip()
    else:
        raise RefundError(422, "请选择核对结果")
    rr.review_note = note or rr.review_note
    rr.updated_at = utcnow()
    db.add(AuditEvent(actor=who, action="REFUND_REQUEST_RESOLVED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "outcome": outcome, "refund_id": rr.ebay_refund_id, "note": note}))
    db.flush()
    return rr


# ------------------------------------------------------------------ 调 eBay
def build_body(rr: RefundRequest, live_lines: dict[str, dict] | None = None) -> dict:
    body: dict = {"reasonForRefund": rr.reason}
    if rr.comment:
        body["comment"] = rr.comment[:MAX_COMMENT]
    if rr.lines:
        body["refundItems"] = [{"lineItemId": x["line_item_id"],
                                "refundAmount": {"value": f"{x['amount']:.2f}", "currency": rr.currency}} for x in rr.lines]
    else:
        body["orderLevelRefundAmount"] = {"value": f"{rr.amount:.2f}", "currency": rr.currency}
    return body


def _live_order(store_id: str, order_id: str) -> dict:
    from .sources import get_ebay_source

    o = get_ebay_source().invoice_order(store_id, order_id)
    if not o:
        raise RefundError(404, "eBay 上查不到这张订单")
    return o


def check_live(rr: RefundRequest, o: dict) -> None:
    """调 eBay 前用实时订单核对：没有全额退过、金额不超过“订单总额 − 已退”、订单行都在。"""
    st = cac.order_refunds(o)
    if st["payment_status"] == "FULLY_REFUNDED":
        raise RefundError(409, "eBay 显示这张订单已经全额退款，不能再退")
    total = _num(((o.get("pricingSummary") or {}).get("total") or {}).get("value"))
    cur = ((o.get("pricingSummary") or {}).get("total") or {}).get("currency")
    if cur and cur.upper() != rr.currency.upper():
        raise RefundError(409, f"eBay 上的订单币种是 {cur}，和申请单（{rr.currency}）不一致")
    refunded = sum((r["amount"] or {}).get("value") or 0 for r in st["refunds"] if (r.get("status") or "").upper() != "FAILED")
    if total is not None and rr.amount > total - refunded + EPS:
        raise RefundError(409, f"eBay 上这张订单总额 {total:.2f} {cur}，已退 {refunded:.2f}，这次最多还能退 {max(0, total - refunded):.2f}")
    if rr.lines:
        live_ids = {ln.get("lineItemId") for ln in o.get("lineItems") or []}
        if any(x["line_item_id"] not in live_ids for x in rr.lines):
            raise RefundError(409, "eBay 上的订单行和申请单对不上，请重新提交")


def _post_issue_refund(store_id: str, order_id: str, body: dict, http: httpx.Client | None = None) -> dict:
    """POST issue_refund（不重试）。返回 eBay 的响应体；出错抛 RefundError（502 = 不确定 eBay 有没有收到）。
    TODO(1号 case_actions_common)：公共的“带令牌 + 签名 + 不重试 + 错误转中文”的 post 好了以后换成它。"""
    from .ebay.auth import EbayAuthError, for_store
    from .ebay.signing import SigningError, sign_headers, signature_error

    base = get_settings().ebay_api_base.replace("://api.", "://apiz.")
    url = f"{base}/sell/fulfillment/v1/order/{order_id}/issue_refund"
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        token = for_store(store_id, REFUND_SCOPE).access_token()
    except EbayAuthError as e:
        raise RefundError(502, f"拿不到店铺退款令牌（需要 sell.finances 权限）：{e}") from None
    try:
        sig = sign_headers("POST", url, raw)
    except SigningError as e:
        raise RefundError(500, f"数字签名配置有误：{e}") from None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json", **sig}
    try:
        resp = (http or httpx.Client(timeout=30)).post(url, content=raw, headers=headers)
    except httpx.HTTPError as e:
        raise RefundError(502, f"请求 eBay 出错（{type(e).__name__}），不确定是否已退款，请到 eBay 后台核对") from None
    text = resp.text or ""
    if resp.status_code >= 500:
        raise RefundError(502, f"eBay 返回 HTTP {resp.status_code}，不确定是否已退款，请到 eBay 后台核对")
    if resp.status_code not in (200, 201):
        from .ebay.signing import signature_error_code

        hint = signature_error(text)
        code = signature_error_code(text)
        try:
            errs = resp.json().get("errors") or []
            msg = "；".join(str(e.get("longMessage") or e.get("message") or e) for e in errs)[:300]
        except ValueError:
            msg = text[:300]
        err = RefundError(422, hint or f"eBay 没有接受退款（HTTP {resp.status_code}）：{msg or '无说明'}")
        err.signature_code = code  # 数字签名错误码：审计、用量统计单独记
        raise err
    try:
        return resp.json() if text else {}
    except ValueError:
        return {}


def execute(db: Session, rr: RefundRequest, user, *, http: httpx.Client | None = None) -> RefundRequest:
    """调 eBay 退款。eBay 明确拒绝或不确定时记“处理异常”（不抛），参数 / 状态不对抛 RefundError。"""
    if not is_senior(user):
        raise RefundError(403, "退款只有管理员和售后可以做")
    reason = cannot_act(db.get(Store, rr.store_id))
    if reason:
        raise RefundError(403, reason)
    via_return = (rr.execution or ORDER) == RETURN
    open_case = open_case_block(db, rr.store_id, rr.order_id, return_id=rr.return_id if via_return else None,
                                allow_case_id=rr.case_id if via_return else None, via_return=via_return)  # 提交后才开的退货 / 纠纷：审核时再挡一次
    if open_case:
        raise RefundError(409, open_case)
    if via_return:
        return _execute_via_return(db, rr, user, http=http)
    try:  # 实时核对订单（查不到就不退，免得重复退款）；这一步失败申请单保持待审核
        live = _live_order(rr.store_id, rr.order_id)
    except RefundError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RefundError(502, f"查不到 eBay 上这张订单的退款情况（{str(e)[:120]}），请稍后再试") from None
    check_live(rr, live)
    who = user.display_name[:64]
    rr.executed_by, rr.executed_at, rr.updated_at = who, utcnow(), utcnow()
    body = build_body(rr)
    try:
        res = _post_issue_refund(rr.store_id, rr.order_id, body, http)
    except RefundError as e:
        rr.status, rr.error = R.ERROR, e.message
        code = getattr(e, "signature_code", None)
        db.add(AuditEvent(actor=who, action="REFUND_ISSUE_FAILED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                          detail={"id": rr.id, "status": e.status_code, "error": e.message, "body": body,
                                  **({"signature_error_code": code} if code else {})}))
        if code:
            record_signature_error(rr.store_id, "issueRefund", code, who, db=db)
        db.flush()
        return rr
    status = (res.get("refundStatus") or "PENDING").upper()
    rr.ebay_refund_id = (res.get("refundId") or "")[:64] or None
    rr.ebay_refund_status = status
    if status == "FAILED":
        rr.status, rr.error = R.ERROR, "eBay 返回退款失败（refundStatus=FAILED），请到 eBay 后台查看原因"
        db.add(AuditEvent(actor=who, action="REFUND_ISSUE_FAILED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                          detail={"id": rr.id, "response": res}))
        db.flush()
        return rr
    rr.status = R.REFUNDED if status == "REFUNDED" else R.REFUNDING
    rr.error = None
    db.add(AuditEvent(actor=who, action="REFUND_ISSUED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "order_id": rr.order_id, "amount": rr.amount, "currency": rr.currency,
                              "refund_id": rr.ebay_refund_id, "refund_status": status, "body": body}))
    _after_refund(db, rr, user)
    db.flush()
    return rr


def _execute_via_return(db: Session, rr: RefundRequest, user, *, http: httpx.Client | None = None) -> RefundRequest:
    """通过退货退款：调 1 号 return_actions.execute_refund_request（实时核对、Post-Order 退款 / 提议部分退款、
    record_action 写审计 / 处理备注 / 会话事件都在那边做，这里不重复写），这里只记申请单状态和 REFUND_ISSUED 审计。
    403 / 404 / 409（没执行）原样抛出，申请单保持待审核；422（eBay 拒绝）/ 502（不确定）记“处理异常”。"""
    from . import return_actions

    fn = getattr(return_actions, "execute_refund_request", None)
    if fn is None:
        raise RefundError(409, "通过退货退款的执行功能还没上线，请先在退货的纠纷处理页里退款")
    who = cac.who(user)
    rr.executed_by, rr.executed_at, rr.updated_at = who, utcnow(), utcnow()
    try:
        res = fn(db, rr, user, http=http) or {}
    except RefundError as e:
        if e.status_code in (403, 404, 409):
            rr.executed_by = rr.executed_at = None
            raise
        rr.status, rr.error = R.ERROR, e.message
        code = getattr(e, "signature_code", None)
        db.add(AuditEvent(actor=who, action="REFUND_ISSUE_FAILED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                          detail={"id": rr.id, "execution": RETURN, "return_id": rr.return_id, "status": e.status_code,
                                  "error": e.message, **({"signature_error_code": code} if code else {})}))
        if code:
            record_signature_error(rr.store_id, getattr(e, "endpoint", None) or "return.refund", code, who, db=db)
        db.flush()
        return rr
    state = str(res.get("state") or "refunded").lower()
    rr.status = RETURN_STATE.get(state, R.REFUNDING)
    if state != "offered":
        _mark_order(db, rr)
    rr.ebay_refund_id = (str(res.get("refund_id") or "")[:64]) or rr.ebay_refund_id
    rr.ebay_refund_status = state.upper()
    rr.error = "已向买家提出部分退款，等买家在 eBay 上同意" if state == "offered" else None
    db.add(AuditEvent(actor=who, action="REFUND_ISSUED", store_id=rr.store_id, conversation_id=rr.conversation_id,
                      detail={"id": rr.id, "execution": RETURN, "return_id": rr.return_id, "amount": rr.amount,
                              "currency": rr.currency, "state": state, "refund_id": rr.ebay_refund_id,
                              "summary": str(res.get("summary") or "")[:300]}))
    db.flush()
    return rr


def record_signature_error(store_id: str, endpoint: str, code: str, actor: str, db: Session | None = None) -> None:
    """eBay 拒绝了数字签名（215xxx）：单独记审计 EBAY_SIGNATURE_ERROR 和用量统计（purpose=ebay_signature，error 字段写错误码），
    方便事后查是哪个店铺、哪个接口被拒。不记密钥和签名头。db 为空时用独立事务写（调用方之后会回滚的场景）。"""
    from .db import session_scope
    from .usage import record

    def write(s: Session) -> None:
        s.add(AuditEvent(actor=actor[:64], action="EBAY_SIGNATURE_ERROR", store_id=store_id,
                         detail={"endpoint": endpoint, "error_code": code}))
        record(s, "ebay_signature", provider="ebay", model=endpoint, ok=False, error=f"{code} {endpoint}", store_id=store_id)

    try:
        if db is not None:
            write(db)
        else:
            with session_scope() as s:
                write(s)
    except Exception:  # noqa: BLE001
        log.exception("记数字签名错误失败：%s %s", store_id, code)


# ------------------------------------------------------------------ 成功后的联动
def amount_text(rr: RefundRequest) -> str:
    return f"{rr.currency} {rr.amount:.2f}"


def summary_text(rr: RefundRequest, checked_by_hand: bool = False) -> str:
    """写进会话事件 / 处理备注的一句话（AI 起草时从会话状态卡里看到）。"""
    kind = "全额退款" if rr.full else "部分退款"
    tail = ("，到 eBay 后台核对过" if checked_by_hand else
            "，eBay 退款处理中，一般几分钟到 1 个工作日到账" if rr.status == R.REFUNDING else "")
    return (f"已退款 {amount_text(rr)}（{kind}，订单 {rr.order_id}，原因：{REASONS.get(rr.reason, rr.reason)}"
            f"，退款申请 #{rr.id}{('，eBay 退款 ID ' + rr.ebay_refund_id) if rr.ebay_refund_id else ''}{tail}）")


def _mark_order(db: Session, rr: RefundRequest) -> None:
    """本地订单马上标有退款（售后原因表不用等财务流水同步）。"""
    o = _order(db, rr.store_id, rr.order_id)
    if o is not None:
        o.refunded = True


def _after_refund(db: Session, rr: RefundRequest, user, checked_by_hand: bool = False) -> None:
    """退款成功（或人工核对确认已退）后：
    - 有关联售后记录（退货 / 纠纷 / 咨询 / 取消）：case_actions_common.record_action（审计、处理备注、处理状态、
      会话系统事件 source=case_action、会话售后状态 / 备注）；
    - 主动退款（没有关联记录）：同样的会话系统事件和会话售后备注，直接写在申请单的会话上。
    售后原因由接口层在提交后调 refresh_reasons_later 重算。"""
    summary = summary_text(rr, checked_by_hand)
    _mark_order(db, rr)
    case = db.get(EbayCase, rr.case_id) if rr.case_id else None
    if case is not None:
        conv = cac.record_action(db, case, action="refund", label="退款", summary=summary, user=user, work_state="DONE")
        if conv is not None and not rr.conversation_id:
            rr.conversation_id = conv.id
        return
    conv = db.get(Conversation, rr.conversation_id) if rr.conversation_id else None
    if conv is None:
        return
    actor, now = cac.who(user), utcnow()
    db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type="SELLER_REFUND_ISSUED",
                             source=cac.EVENT_SOURCE, case_id=f"refund_request:{rr.id}", detail=summary[:1000], occurred_at=now))
    conv.row_version += 1  # 之前生成的草稿过期，下一次起草能看到这次退款
    conv.aftersales_status = "IN_PROGRESS" if conv.aftersales_status != "DONE" else conv.aftersales_status
    conv.aftersales_type = conv.aftersales_type or "退款"
    line = f"[{now.strftime('%m-%d %H:%M')} {actor}] {summary}"
    conv.aftersales_note = (f"{conv.aftersales_note}\n{line}" if conv.aftersales_note else line)[-4000:]
    conv.aftersales_handled_by = conv.aftersales_handled_by or actor
    conv.aftersales_updated_at = now


def refresh_reasons_later(store_id: str) -> None:
    """提交后在后台线程重算这家店的售后原因（case_actions_common.refresh_reasons；退款信号由 aftersales_reasons
    读 refund_requests）。测试里不起线程。"""
    import sys

    if not BACKGROUND or "pytest" in sys.modules:
        return
    threading.Thread(target=cac.refresh_reasons, args=(store_id,), name=f"refund-reasons-{store_id}", daemon=True).start()


# ------------------------------------------------------------------ 和退款流水对上
OVERDUE = timedelta(days=3)  # 退款中超过这么久还对不上退款流水：记“处理异常”，列表高亮


def _refund_ids_in(c: EbayCase) -> set[str]:
    """退款流水 detail.references 里的编号（财务接口可能带退款 ID）。"""
    return {str(r.get("id")) for r in ((c.detail or {}).get("references") or []) if r.get("id")}


def reconcile(db: Session, store_id: str | None = None, now=None) -> int:
    """“退款中”的申请和退款流水（Finances，售后同步拉 REFUND 后跑）对上：先按流水里带的退款 ID（references），
    再按同一订单、调 eBay 之后（前 1 小时起）的第一笔没被占用的流水。对上记“已退款”；超过 3 天还对不上记“处理异常”。
    返回对上的数量。"""
    now = now or utcnow()
    q = select(R).where(R.status == R.REFUNDING)
    if store_id:
        q = q.where(R.store_id == store_id)
    n = 0
    for rr in list(db.scalars(q)):
        used = set(db.scalars(select(R.refund_case_id).where(R.refund_case_id.is_not(None))))
        flows = list(db.scalars(select(EbayCase).where(EbayCase.kind == "REFUND", EbayCase.store_id == rr.store_id,
                                                       EbayCase.order_id == rr.order_id).order_by(EbayCase.opened_at)))
        flows = [c for c in flows if c.id not in used]
        since = (rr.executed_at or rr.submitted_at) - timedelta(hours=1)
        hit = next((c for c in flows if rr.ebay_refund_id and rr.ebay_refund_id in _refund_ids_in(c)), None)             or next((c for c in flows if c.opened_at and c.opened_at >= since), None)
        if hit is not None:
            rr.status, rr.refund_case_id, rr.ebay_refund_status, rr.updated_at = R.REFUNDED, hit.id, "REFUNDED", now
            n += 1
        elif (rr.executed_at or rr.submitted_at) < now - OVERDUE:
            rr.status, rr.updated_at = R.ERROR, now
            rr.error = (f"eBay 接受退款已超过 3 天（{(rr.executed_at or rr.submitted_at):%Y-%m-%d %H:%M} UTC），还没有对上退款流水："
                        "请到 eBay 后台看这笔退款有没有到账，再标“已退款”或“没退，退回待审核”")
    db.flush()
    return n


def refresh_live(db: Session, rr: RefundRequest) -> RefundRequest:
    """实时查订单：这笔 refundId 在 eBay 上已经退了就记为已退款（只读）。"""
    if rr.status != R.REFUNDING or not rr.ebay_refund_id:
        return rr
    o = _live_order(rr.store_id, rr.order_id)
    raw = [*((o.get("paymentSummary") or {}).get("refunds") or []),
           *(r for ln in o.get("lineItems") or [] for r in ln.get("refunds") or [])]
    hit = next((r for r in raw if str(r.get("refundId") or "") == rr.ebay_refund_id), None)
    if hit:
        st = (hit.get("refundStatus") or "").upper()
        rr.ebay_refund_status = st or rr.ebay_refund_status
        if st == "REFUNDED":
            rr.status = R.REFUNDED
        elif st == "FAILED":
            rr.status, rr.error = R.ERROR, "eBay 显示这笔退款失败，请到 eBay 后台查看原因"
    elif cac.order_refunds(o)["payment_status"] == "FULLY_REFUNDED" and rr.full:
        rr.status, rr.ebay_refund_status = R.REFUNDED, "REFUNDED"
    rr.updated_at = utcnow()
    db.flush()
    return rr


# ------------------------------------------------------------------ 输出
def to_dict(rr: RefundRequest, names: dict[str, str] | None = None) -> dict:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    return {
        "id": rr.id, "store_id": rr.store_id, "store_name": (names or {}).get(rr.store_id, rr.store_id), "order_id": rr.order_id,
        "buyer": rr.buyer, "conversation_id": rr.conversation_id, "source": rr.source, "source_label": SOURCES.get(rr.source, rr.source),
        "case_id": rr.case_id, "case_kind": rr.case_kind, "case_external_id": rr.case_external_id,
        "full": bool(rr.full), "full_label": "全额退款" if rr.full else "部分退款", "amount": rr.amount, "currency": rr.currency,
        "lines": rr.lines or [], "reason": rr.reason, "reason_label": REASONS.get(rr.reason, rr.reason),
        "execution": rr.execution or ORDER, "execution_label": EXECUTIONS.get(rr.execution or ORDER), "return_id": rr.return_id,
        "return_payload": rr.return_payload,
        "comment": rr.comment, "note": rr.note, "order_total": rr.order_total, "order_currency": rr.order_currency,
        "review_mode": rr.review_mode, "status": rr.status, "status_label": R.STATUS_LABELS.get(rr.status, rr.status),
        "submitted_by": rr.submitted_by, "submitted_at": iso(rr.submitted_at), "reviewed_by": rr.reviewed_by,
        "reviewed_at": iso(rr.reviewed_at), "review_note": rr.review_note, "executed_by": rr.executed_by,
        "executed_at": iso(rr.executed_at), "ebay_refund_id": rr.ebay_refund_id, "ebay_refund_status": rr.ebay_refund_status,
        "error": rr.error, "refund_case_id": rr.refund_case_id, "updated_at": iso(rr.updated_at),
    }
