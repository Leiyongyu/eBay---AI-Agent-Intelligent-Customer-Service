"""退款单页面用的补充信息（全部读库，不调 eBay）。

eBay 财务接口的退款流水（EbayCase.kind == "REFUND"）只有交易号、订单号、买家、结算金额（多为美元）、结算状态。
页面上要的“退款单类型、纠纷编号、退款原因、当地币种金额、全额 / 部分退款、SKU”从同一订单的其他售后记录和订单里推：
- 退款单类型：同一订单有退货 / 纠纷 / 未收到货咨询 / 付款纠纷 = 纠纷退货；有取消 = 取消交易；都没有 = 主动退款（卖家直接退）。
- 纠纷编号、退款原因：取关联的那条记录（退货 > 纠纷 > 咨询 > 付款纠纷 > 取消）。
- 当地币种金额：流水的退款总额 totalFeeBasisAmount（换算前的当地原额，或本来就是订单币种；2026-11-12 之后拉到的才有）。
  注意流水的 amount 是扣回费用后的结算净额（全额退款约为订单额的 85%），不能直接和订单总额比；关联退货单上的金额是
  eBay 的预估退款额，也不一定是实际退的，只在弹窗里列出来参考。
- 全额 / 部分：有当地退款总额时和订单总额（同币种）比，差 0.01 以内算全额；旧数据只有结算净额时按比例估（FULL_RATIO，
  标 estimated）；其余不判断。
"""
from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import EbayCase, EbayOrder, EbayOrderLine, RefundRequest

TYPES = {"aftersales": "纠纷退货", "cancel": "取消交易", "manual": "主动退款"}
FULL_LABELS = {"full": "全额退款", "partial": "部分退款"}
KIND_LABELS = {"RETURN": "退货", "CASE": "纠纷", "INQUIRY": "未收到货咨询", "PAYMENT_DISPUTE": "付款纠纷", "CANCEL": "取消交易"}
_PRIORITY = ("RETURN", "CASE", "INQUIRY", "PAYMENT_DISPUTE", "CANCEL")
_CHUNK = 500


def _chunks(values) -> list[list]:
    v = list(values)
    return [v[i:i + _CHUNK] for i in range(0, len(v), _CHUNK)]


# 只有结算净额（美元）时的估算：全额退款的“净额 ÷ 订单总额”落在这个区间（扣回费用约 15%，再乘汇率；
# 按 2026-08～11 正式库里关联记录确认全额的退款统计），低于下限算部分退款，其他币种不估
FULL_RATIO = {"USD": (0.78, 1.02), "EUR": (0.78, 1.08), "GBP": (1.0, 1.3)}


def _local_and_full(c: EbayCase, d: dict, order: EbayOrder | None, refunds_in_order: int):
    """当地币种退款额、来源（ebay 准确 / estimate 估算）、全额 / 部分。"""
    total, cur = (order.total, order.currency) if order is not None else (None, None)
    local, local_cur = None, None
    if d.get("local_amount") is not None and d.get("local_currency"):
        local, local_cur = float(d["local_amount"]), d["local_currency"]
    elif d.get("gross_amount") is not None and d.get("gross_currency") and d["gross_currency"] == cur:
        local, local_cur = float(d["gross_amount"]), d["gross_currency"]
    if local is not None:
        full = ("full" if local >= total - 0.01 else "partial") if total and local_cur == cur else None
        return local, local_cur, "ebay", full
    # 旧数据只有结算净额：按比例估
    band = FULL_RATIO.get(cur or "")
    if band and total and c.amount is not None and c.currency == "USD" and refunds_in_order == 1:
        ratio = c.amount / total
        if band[0] <= ratio <= band[1]:
            return total, cur, "estimate", "full"
        if ratio < band[0]:
            return None, None, "estimate", "partial"
    return None, None, None, None


def refund_info(db: Session, cases: list[EbayCase]) -> dict[int, dict]:
    """一批退款流水的补充信息：{case.id: {...}}。几次批量查询。"""
    from .case_labels import reason_label, status_label

    refunds = [c for c in cases if c.kind == "REFUND"]
    if not refunds:
        return {}
    stores = {c.store_id for c in refunds}
    oids = {c.order_id for c in refunds if c.order_id}

    orders: dict[tuple[str, str], EbayOrder] = {}
    lines: dict[tuple[str, str], list[EbayOrderLine]] = defaultdict(list)
    line_owner: dict[tuple[str, str], str] = {}  # (店铺, 商品号-交易号) → 订单号：纠纷、咨询用这种订单号
    linked: dict[tuple[str, str], list[EbayCase]] = defaultdict(list)
    for chunk in _chunks(oids):
        for o in db.scalars(select(EbayOrder).where(EbayOrder.store_id.in_(stores), EbayOrder.order_id.in_(chunk))):
            orders[(o.store_id, o.order_id)] = o
        for ln in db.scalars(select(EbayOrderLine).where(EbayOrderLine.store_id.in_(stores), EbayOrderLine.order_id.in_(chunk))):
            lines[(ln.store_id, ln.order_id)].append(ln)
            if ln.line_key:
                line_owner[(ln.store_id, ln.line_key)] = ln.order_id
        for x in db.scalars(select(EbayCase).where(EbayCase.store_id.in_(stores), EbayCase.order_id.in_(chunk),
                                                   EbayCase.kind.in_(("RETURN", "PAYMENT_DISPUTE", "CANCEL")))):
            linked[(x.store_id, x.order_id)].append(x)
    for chunk in _chunks({k[1] for k in line_owner}):
        for x in db.scalars(select(EbayCase).where(EbayCase.store_id.in_(stores), EbayCase.order_id.in_(chunk),
                                                   EbayCase.kind.in_(("CASE", "INQUIRY")))):
            oid = line_owner.get((x.store_id, x.order_id))
            if oid:
                linked[(x.store_id, oid)].append(x)
    per_order = Counter((c.store_id, c.order_id) for c in refunds)
    # 本系统的退款申请单：先按对上的流水（refund_case_id），再按同一订单（已退款 / 退款中）
    reqs_by_case: dict[int, RefundRequest] = {}
    reqs_by_order: dict[tuple[str, str], list[RefundRequest]] = defaultdict(list)
    for chunk in _chunks(oids):
        for r in db.scalars(select(RefundRequest).where(RefundRequest.store_id.in_(stores), RefundRequest.order_id.in_(chunk),
                                                        RefundRequest.status.in_(("REFUNDED", "REFUNDING")))):
            if r.refund_case_id:
                reqs_by_case[r.refund_case_id] = r
            reqs_by_order[(r.store_id, r.order_id)].append(r)

    out: dict[int, dict] = {}
    for c in refunds:
        key = (c.store_id, c.order_id)
        links = sorted(linked.get(key, []), key=lambda x: (_PRIORITY.index(x.kind), -(x.opened_at.timestamp() if x.opened_at else 0)))
        kinds = {x.kind for x in links}
        rtype = "aftersales" if kinds & {"RETURN", "CASE", "INQUIRY", "PAYMENT_DISPUTE"} else "cancel" if "CANCEL" in kinds else "manual"
        main = links[0] if links else None
        order = orders.get(key)
        d = c.detail or {}

        local, local_cur, local_from, full = _local_and_full(c, d, order, per_order[key])

        reason = main.reason if main is not None else None
        skus = [ln.sku for ln in lines.get(key, []) if ln.sku]
        if not skus and order is not None:
            skus = [s.strip() for s in (order.skus or "").split(",") if s.strip()]
        out[c.id] = {
            "type": rtype, "type_label": TYPES[rtype],
            "full": full, "full_label": FULL_LABELS.get(full or ""), "estimated": local_from == "estimate",
            "local_amount": local, "local_currency": local_cur, "local_from": local_from,
            "order_total": order.total if order else None, "order_currency": order.currency if order else None,
            "reason": reason, "reason_label": reason_label(reason) if reason else None,
            "memo": d.get("memo"),
            "skus": list(dict.fromkeys(skus)),
            "linked": [{"id": x.id, "kind": x.kind, "kind_label": KIND_LABELS.get(x.kind, x.kind), "external_id": x.external_id,
                        "reason": x.reason, "reason_label": reason_label(x.reason) if x.reason else None,
                        "status": x.status, "status_label": status_label(x.status), "amount": x.amount, "currency": x.currency,
                        "action_url": (x.detail or {}).get("action_url")} for x in links],
            "refunds_in_order": per_order[key],
            "request": _request_brief(reqs_by_case.get(c.id) or (reqs_by_order.get(key) or [None])[0]),
        }
    return out


def _refund_reason_label(code: str | None) -> str | None:
    from .refund_requests import REASONS

    return REASONS.get(code or "", code)


def _request_brief(r: RefundRequest | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "status": r.status, "submitted_by": r.submitted_by,
            "submitted_at": r.submitted_at.isoformat(timespec="seconds") + "Z" if r.submitted_at else None,
            "reviewed_by": r.reviewed_by, "amount": r.amount, "currency": r.currency, "reason": r.reason,
            "reason_label": _refund_reason_label(r.reason),
            "comment": r.comment, "ebay_refund_id": r.ebay_refund_id}


def matches(info: dict | None, *, rtype: str | None, full: str | None, reason: str | None, linked_q: list[str] | None,
            sku_q: list[str] | None, match: str = "contains") -> bool:
    """退款单页面的筛选：类型、全额 / 部分、原因、纠纷编号、SKU（不区分大小写）。"""
    if info is None:
        return not (rtype or full or reason or linked_q or sku_q)
    if rtype and info["type"] != rtype:
        return False
    if full and info["full"] != full:
        return False
    if reason and (info["reason"] or "") != reason:
        return False

    def hit(values: list[str], words: list[str]) -> bool:
        vs = [v.lower() for v in values if v]
        for w in (x.lower() for x in words):
            if any(v == w if match == "exact" else v.startswith(w) if match == "prefix" else w in v for v in vs):
                return True
        return False

    if linked_q and not hit([x["external_id"] for x in info["linked"]], linked_q):
        return False
    if sku_q and not hit(info["skus"], sku_q):
        return False
    return True
