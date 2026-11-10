"""订单维度的售后原因最终分类（order_aftersales_reasons）：统计订单维度、SKU 维度的退款率，看哪种原因退款率高。

进表范围：ebay_orders 近 WINDOW_DAYS 天（订单同步范围）内、所有有订单数据的店铺，有任一售后信号的订单——
  退款（订单标记 refunded 或 eBay 退款单）、退货、未收到货咨询、纠纷、付款纠纷、取消；
  或关联会话被转售后（conversations.aftersales_status）、飞书售后工单、异常包裹跟进、谷仓查件单。
  退款率分母是同范围内的全部订单（ebay_orders），不只是进表的。

售后单怎么对到订单：退货 / 退款单 / 付款纠纷 / 部分取消单的订单号就是新格式订单号；咨询、纠纷（和部分取消单）是
“商品号-交易号”，按 ebay_order_lines.line_key / legacy_order_id 换成订单号。
会话怎么对到订单：同店铺、同买家（不分大小写），会话关联的刊登在订单商品里，或是按订单建的会话（ORDER-订单号）；
飞书工单、异常包裹、查件单自带的会话也算。买家在这个店铺只有一张订单时，没关联刊登的会话也归到这张订单。

AI 自动分类：只用规则（不调用大模型），按依据优先级取第一个能判的（RULES，数字越小越优先）：
  1 eBay 退货原因（RETURN_REASON）；ARRIVED_DAMAGED 有发错 / 漏放信号（飞书“破损错发缺件”、包裹跟进“谷仓发错”）算仓库原因，
    否则标物流问题并写“需核实是运输还是包装”；
  2 付款纠纷原因（DISPUTE_REASON）；
  3 物流信号：未收到货咨询、类型为未收到货（ITEM_NOT_RECEIVED）的纠纷、异常包裹跟进（断更、无轨迹、妥投未收到、退回、丢失…，PARCEL_REASON）、谷仓查件单 → 物流问题；
  4 取消原因（CANCEL_REASON）：买家下错单、找到更便宜、地址 / 付款方式错 → 客户原因；来不及送达 → 物流问题；缺货发不了 → listing问题；
  5 飞书售后工单类别（FEISHU_CATEGORY）；
  6 AI 草稿意图（INTENT_REASON；fitment 只在订单有退货 / 退款时算不适配）；
  7 客服转售后时标的问题类型（只认“不适配”）。
  都判不出来留空（UNCLASSIFIED），ai_note 写缺什么信息。
人工标记（manual_*）永远优先，AI 重算不覆盖；最终原因 final_reason = 人工 > AI > 空。
什么时候算：每小时售后同步（ebay_cases.sync_all）每个店铺之后 refresh_store；管理员接口全量重建 rebuild_all。
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import session_scope, utcnow
from .models import (AftersalesCase, Conversation, Draft, EbayCase, EbayOrder, EbayOrderLine, LogisticsTicket,
                     OrderAftersalesReason, ParcelFollowup, ReplyJob, Store)

log = logging.getLogger(__name__)

WINDOW_DAYS = 180
REASONS = {"PRODUCT": "产品问题", "FITMENT": "不适配", "WAREHOUSE": "仓库原因", "LISTING": "listing问题",
           "CUSTOMER": "客户原因", "LOGISTICS": "物流问题"}
STATUS_LABELS = {"UNCLASSIFIED": "未分类", "AI": "AI 分类", "MANUAL": "人工标记"}
EVENT_KINDS = ("RETURN", "INQUIRY", "CASE", "PAYMENT_DISPUTE", "CANCEL", "REFUND")
KIND_LABELS = {"RETURN": "退货", "INQUIRY": "咨询", "CASE": "纠纷", "PAYMENT_DISPUTE": "付款纠纷", "CANCEL": "取消交易",
               "REFUND": "退款单"}
SYSTEM_TYPE = "FROM_EBAY"

# ---------------------------------------------------------------- 规则表
# eBay 退货原因 → 原因代码；None = 判不出来（继续看下一级依据）
RETURN_REASON = {
    "DEFECTIVE_ITEM": "PRODUCT",
    "DOES_NOT_FIT": "FITMENT",
    "WRONG_ITEM": "WAREHOUSE", "MISSING_PARTS": "WAREHOUSE", "ORDERED_DIFFERENT_ITEM": "WAREHOUSE",  # 我方发错 / 漏放
    "NOT_AS_DESCRIBED": "LISTING", "DIFFERENT_FROM_LISTING": "LISTING", "VERSION_NOT_AS_ADVERTISED": "LISTING",
    "NO_LONGER_NEED_ITEM": "CUSTOMER", "ORDERED_WRONG_ITEM": "CUSTOMER",
    "WRONG_SIZE": "FITMENT",  # 尺寸不对：汽配多是车型不匹配，归不适配（用户 2026-11-10 定）
    "FOUND_BETTER_PRICE": "CUSTOMER", "ORDERED_ACCIDENTALLY": "CUSTOMER", "WITHDRAW_FROM_PURCHASE_CONTRACT": "CUSTOMER",
    "BUYER_CANCEL_ORDER": "CUSTOMER", "RETURNING_GIFT": "CUSTOMER", "BUYER_NO_SHOW": "CUSTOMER",
    "ARRIVED_DAMAGED": "LOGISTICS",  # 先看有没有发错 / 漏放信号，见 classify
    "ARRIVED_LATE": "LOGISTICS",
}
DISPUTE_REASON = {"ITEM_NOT_RECEIVED": "LOGISTICS", "SIGNIFICANTLY_NOT_AS_DESCRIBED": "LISTING"}
CANCEL_REASON = {
    "ORDER_MISTAKE": "CUSTOMER", "FOUND_CHEAPER_PRICE": "CUSTOMER", "BUYER_ASKED_CANCEL": "CUSTOMER",
    "WRONG_SHIPPING_ADDRESS": "CUSTOMER", "WRONG_PAYMENT_METHOD": "CUSTOMER", "WRONG_SHIPPING_METHOD": "CUSTOMER",
    "ORDER_UNPAID": "CUSTOMER", "WONT_ARRIVE_IN_TIME": "LOGISTICS", "OUT_OF_STOCK_OR_CANNOT_FULFILL": "LISTING",
}
PARCEL_REASON = {"断更": "LOGISTICS", "无轨迹": "LOGISTICS", "妥投未收到": "LOGISTICS", "物流异常退回": "LOGISTICS",
                 "未收到货纠纷": "LOGISTICS", "丢失": "LOGISTICS", "到货损坏": "LOGISTICS", "谷仓发错": "WAREHOUSE",
                 "供应商发错": "WAREHOUSE", "产品问题": "PRODUCT", "地址问题": "CUSTOMER"}
FEISHU_CATEGORY = {"质量问题": "PRODUCT", "破损错发缺件": "WAREHOUSE", "适配核对": "FITMENT"}
INTENT_REASON = {"quality_issue": "PRODUCT", "damaged_wrong": "WAREHOUSE", "fitment": "FITMENT",
                 "delivery_delay": "LOGISTICS", "cancel_request": "CUSTOMER"}
CONV_TYPE_REASON = {"不适配": "FITMENT"}
RULES = (  # (优先级, 依据前缀, 说明)
    (1, "RETURN", "eBay 退货原因"), (2, "DISPUTE", "付款纠纷原因"),
    (3, "INQUIRY / PARCEL / TICKET", "未收到货咨询、异常包裹跟进、谷仓查件单"), (4, "CANCEL", "取消原因"),
    (5, "FEISHU", "飞书售后工单类别"), (6, "INTENT", "AI 草稿意图"), (7, "CONV", "客服转售后时标的问题类型"),
)


# ---------------------------------------------------------------- 人工文本
_SEP = re.compile(r"^[\s:：,，、\-—]+")


def parse_text(text: str | None) -> tuple[str, str | None]:
    """“产品问题 XXXXXXX” → ("PRODUCT", "XXXXXXX")。开头是 6 种中文名（或代码）之一，后面空格（全角空格、冒号也行）接备注；
    解析不了抛 ValueError。"""
    t = (text or "").replace("　", " ").strip()
    names = sorted([(n, c) for c, n in REASONS.items()] + [(c, c) for c in REASONS], key=lambda x: -len(x[0]))
    for name, code in names:
        if t.lower().startswith(name.lower()):
            rest = t[len(name):]
            if rest and not _SEP.match(rest):
                continue  # “产品问题X”这种粘在一起的不认，免得把别的词当成原因
            note = _SEP.sub("", rest).strip()
            return code, (" ".join(note.split()) or None)
    raise ValueError("开头要是 " + " / ".join(REASONS.values()) + " 之一，后面空格接备注，如“产品问题 外壳开裂”")


def reason_code(value: str | None) -> str:
    """接口里的 reason：代码或中文名都行。"""
    v = (value or "").strip()
    for code, name in REASONS.items():
        if v.upper() == code or v.lower() == name.lower():
            return code
    raise ValueError("原因只能是 " + " / ".join(REASONS.values()))


def apply_final(row: OrderAftersalesReason) -> None:
    row.final_reason = row.manual_reason or row.ai_reason
    row.status = "MANUAL" if row.manual_reason else ("AI" if row.ai_reason else "UNCLASSIFIED")


# ---------------------------------------------------------------- 分类
BASIS_PREFIX = {"RETURN": "退货原因", "DISPUTE": "付款纠纷原因", "INQUIRY": "未收到货咨询", "CASE": "纠纷类型",
                "PARCEL": "异常包裹跟进", "TICKET": "谷仓查件单", "CANCEL": "取消原因", "FEISHU": "飞书售后工单",
                "INTENT": "AI 草稿意图", "CONV": "客服转售后标记"}
CASE_TYPE_LABELS = {"ITEM_NOT_RECEIVED": "未收到货", "RETURN": "退货升级"}
INTENT_LABELS = {"quality_issue": "质量问题", "damaged_wrong": "破损 / 错发", "fitment": "适配", "delivery_delay": "物流延误",
                 "cancel_request": "取消订单", "return_refund": "退货退款", "shipping_status": "物流查询"}
FEISHU_OVERRIDES_CUSTOMER = {"质量问题", "破损错发缺件"}  # 买家自选的客户类退货原因 vs 工单记了质量 / 破损：以工单为准


def basis_label(b: str) -> str:
    """依据 → 中文，如 RETURN:DOES_NOT_FIT → 退货原因：不适配（DOES_NOT_FIT）。"""
    kind, _, val = b.partition(":")
    name = BASIS_PREFIX.get(kind, kind)
    if not val:
        return name
    if kind in ("RETURN", "CANCEL", "DISPUTE"):
        try:
            from .case_labels import reason_label

            zh = reason_label(val)
        except Exception:  # noqa: BLE001
            zh = None
        return f"{name}：{zh}（{val}）" if zh and zh != val else f"{name}：{val}"
    if kind == "CASE":
        return f"{name}：{CASE_TYPE_LABELS.get(val, val)}"
    if kind == "INTENT":
        return f"{name}：{INTENT_LABELS.get(val, val)}"
    return f"{name}：{val}"


def classify(sig: dict) -> tuple[str | None, str | None, list[str]]:
    """sig：一张订单的售后信号。返回 (原因代码, 说明, 依据列表)。依据按优先级排，第一个能判的定原因。"""
    ev: list[tuple[int, str, str | None, str | None]] = []  # (优先级, 依据, 原因, 说明)
    warehouse_hint = any(c == "破损错发缺件" for c in sig["feishu"]) or any(p in ("谷仓发错", "供应商发错") for p in sig["parcel"])
    for r in sig["returns"]:
        code, note = RETURN_REASON.get(r or ""), None
        if r == "ARRIVED_DAMAGED":
            code, note = ("WAREHOUSE", "到货损坏，另有发错 / 漏放信号") if warehouse_hint else \
                ("LOGISTICS", "到货损坏：需核实是运输还是包装")
        ev.append((1, f"RETURN:{r or 'NO_REASON'}", code, note))
    for r in sig["disputes"]:
        ev.append((2, f"DISPUTE:{r or '-'}", DISPUTE_REASON.get(r or ""), None))
    if sig["inquiry"]:
        ev.append((3, "INQUIRY", "LOGISTICS", "买家开了未收到货咨询"))
    for ct in sig.get("case_types") or []:
        if ct == "ITEM_NOT_RECEIVED":
            ev.append((3, "CASE:ITEM_NOT_RECEIVED", "LOGISTICS", "买家开了未收到货纠纷"))
        elif ct:
            ev.append((3, f"CASE:{ct}", None, None))  # 退货升级的纠纷：看退货原因
    for p in sig["parcel"]:
        ev.append((3, f"PARCEL:{p}", PARCEL_REASON.get(p), None))
    for t in sig["tickets"]:
        ev.append((3, f"TICKET:{t or '查件'}", "LOGISTICS", "有谷仓查件单"))
    for r in sig["cancels"]:
        ev.append((4, f"CANCEL:{r or '-'}", CANCEL_REASON.get(r or ""), None))
    for c in sig["feishu"]:
        ev.append((5, f"FEISHU:{c}", FEISHU_CATEGORY.get(c), None))
    has_return = bool(sig["returns"]) or sig["refunded"]
    for i in sig["intents"]:
        code = INTENT_REASON.get(i)
        if i == "fitment" and not has_return:
            code = None  # 只问适配、没退货退款的不算
        ev.append((6, f"INTENT:{i}", code, None))
    for t in sig["conv_types"]:
        ev.append((7, f"CONV:{t}", CONV_TYPE_REASON.get(t), None))
    ev.sort(key=lambda x: x[0])
    basis = list(dict.fromkeys(b for _, b, _, _ in ev))
    for _, b, code, note in ev:
        if code:
            if b.startswith("RETURN:") and code == "CUSTOMER":
                cat = next((c for c in sig["feishu"] if c in FEISHU_OVERRIDES_CUSTOMER), None)
                if cat:  # 买家自选“客户原因”，但飞书工单记了质量问题 / 破损：以工单为准
                    conflict = f"冲突：{basis_label(b)}；以飞书工单为准：{cat}"
                    return FEISHU_CATEGORY[cat], conflict, [conflict, *basis]
            return code, note or basis_label(b), basis
    return None, _missing_note(sig), basis


def _missing_note(sig: dict) -> str:
    """没分类的原因（页面上能看出为什么没分类）。"""
    parts = []
    if sig["returns"]:
        bad = [r for r in sig["returns"] if not RETURN_REASON.get(r or "")]
        parts.append("退货原因为 " + "、".join(r or "NO_REASON" for r in bad) + "（买家没写原因或无法归到 6 类）")
    if sig["disputes"]:
        parts.append("付款纠纷原因 " + "、".join(r or "-" for r in sig["disputes"]) + " 无法归到 6 类（欺诈 / 交易问题 / 没收到退款）")
    types = [t for t in sig.get("case_types") or [] if t]
    if sig["case"] and not sig["inquiry"] and not types:
        parts.append("有纠纷但不知道类型（纠纷详情没有拉取）")
    elif "RETURN" in types and not sig["returns"]:
        parts.append("纠纷是退货升级来的，但没有拉到那张退货单，缺退货原因")
    if sig["cancels"]:
        parts.append("取消原因 " + "、".join(r or "-" for r in sig["cancels"]) + " 无法归到 6 类")
    if not parts and sig["refunded"]:
        since = sig.get("return_from")
        at = sig.get("order_at")
        if since is None:
            parts.append("只有退款记录，缺少退货原因（这个店铺没有拉到任何退货单）")
        elif at is not None and at < since:
            parts.append(f"只有退款记录，缺少退货原因（早于售后同步范围：这个店铺的退货单最早只到 {since:%Y-%m-%d}）")
        else:
            parts.append("只有退款记录，没有退货单（可能是直接退款或部分退款）")
    if not parts:
        parts.append("只有售后标记，没有退货 / 纠纷原因")
    parts.append("有关联会话，但没有能判断的意图、工单或标记" if sig["conv_ids"] else "没有关联会话（会话已清空或对不上订单），缺少买家描述")
    return "；".join(parts)


# ---------------------------------------------------------------- 收集信号
def _empty_sig() -> dict:
    return {"returns": [], "disputes": [], "cancels": [], "inquiry": False, "case": False, "case_types": [], "refunded": False,
            "refund_amount": 0.0, "refund_currency": None, "refund_at": None, "events": [], "parcel": [], "tickets": [],
            "feishu": [], "intents": [], "conv_types": [], "conv_ids": [], "primary_conv": None, "aftersales_conv": False,
            "baseline_conv_ids": []}


def collect_store(db: Session, store_id: str, now: datetime | None = None) -> tuple[dict[str, EbayOrder], dict[str, dict]]:
    """一个店铺近 WINDOW_DAYS 天的订单，和其中有售后信号的订单的信号。"""
    now = now or utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    orders = {o.order_id: o for o in db.scalars(select(EbayOrder).where(EbayOrder.store_id == store_id,
                                                                         EbayOrder.created_at >= since))}
    if not orders:
        return orders, {}
    alias: dict[str, str] = {}
    for oid, key, legacy in db.execute(select(EbayOrderLine.order_id, EbayOrderLine.line_key, EbayOrderLine.legacy_order_id)
                                       .where(EbayOrderLine.store_id == store_id)):
        for a in (key, legacy):
            if a and oid in orders:
                alias[a] = oid

    def order_of(ref: str | None) -> str | None:
        if not ref:
            return None
        return ref if ref in orders else alias.get(ref)

    sigs: dict[str, dict] = defaultdict(_empty_sig)
    return_from = db.scalar(select(func.min(EbayCase.opened_at)).where(EbayCase.store_id == store_id, EbayCase.kind == "RETURN"))
    for c in db.scalars(select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.kind.in_(EVENT_KINDS))):
        oid = order_of(c.order_id)
        if oid is None:
            continue
        s = sigs[oid]
        s["events"].append({"kind": c.kind, "id": c.external_id, "reason": c.reason, "status": c.status,
                            "at": c.opened_at.isoformat(timespec="seconds") + "Z" if c.opened_at else None})
        if c.kind == "RETURN":
            s["returns"].append(c.reason)
        elif c.kind == "PAYMENT_DISPUTE":
            s["disputes"].append(c.reason)
        elif c.kind == "CANCEL":
            s["cancels"].append(c.reason)
        elif c.kind == "INQUIRY":
            s["inquiry"] = True
        elif c.kind == "CASE":
            s["case"] = True
            s["case_types"].append(c.reason)  # 纠纷类型（case_backfill 查详情补的 caseType）
        elif c.kind == "REFUND":
            s["refunded"] = True
            s["refund_amount"] += abs(c.amount or 0)
            s["refund_currency"] = s["refund_currency"] or c.currency
            if c.opened_at and (s["refund_at"] is None or c.opened_at > s["refund_at"]):
                s["refund_at"] = c.opened_at
    for oid, o in orders.items():
        if o.refunded:
            sigs[oid]["refunded"] = True
    for oid, reason, conv_id in db.execute(select(ParcelFollowup.order_id, ParcelFollowup.reason, ParcelFollowup.conversation_id)
                                           .where(ParcelFollowup.store_id == store_id)):
        if (oid := order_of(oid)) is not None:
            sigs[oid]["parcel"].append(reason)
            if conv_id:
                sigs[oid]["primary_conv"] = sigs[oid]["primary_conv"] or conv_id
    for ref, type_name, conv_id, st in db.execute(select(LogisticsTicket.reference_no, LogisticsTicket.type_name,
                                                         LogisticsTicket.conversation_id, LogisticsTicket.store_id)):
        if st not in (None, store_id) or (oid := order_of(ref)) is None:
            continue
        sigs[oid]["tickets"].append(type_name)
        if conv_id:
            sigs[oid]["primary_conv"] = sigs[oid]["primary_conv"] or conv_id
    for oid, cat, conv_id in db.execute(select(AftersalesCase.order_id, AftersalesCase.category, AftersalesCase.conversation_id)
                                        .where(AftersalesCase.store_id == store_id, AftersalesCase.order_id.is_not(None))):
        if (oid := order_of(oid)) is not None:
            sigs[oid]["feishu"].append(cat)
            sigs[oid]["primary_conv"] = conv_id or sigs[oid]["primary_conv"]

    # 会话 ↔ 订单
    by_buyer: dict[str, list[str]] = defaultdict(list)
    for oid, o in orders.items():
        if o.buyer:
            by_buyer[o.buyer.lower()].append(oid)
    convs = db.execute(select(Conversation.id, Conversation.buyer_key, Conversation.item_id, Conversation.ebay_conversation_id,
                              Conversation.aftersales_status, Conversation.aftersales_type, Conversation.last_message_at)
                       .where(Conversation.store_id == store_id, Conversation.conversation_type != SYSTEM_TYPE,
                              Conversation.buyer_key.is_not(None))).all()
    conv_orders: dict[int, list[str]] = {}
    for c in convs:
        mine = by_buyer.get((c.buyer_key or "").lower(), [])
        hit = [oid for oid in mine if c.ebay_conversation_id == f"ORDER-{oid}"
               or (c.item_id and c.item_id in (orders[oid].item_ids or orders[oid].item_id or "").split(","))]
        if not hit and len(mine) == 1 and not c.item_id:
            hit = mine
        if hit:
            conv_orders[c.id] = hit
        for oid in hit:
            s = sigs[oid] if (c.aftersales_status or oid in sigs) else None
            if s is None:
                continue
            s["conv_ids"].append((c.last_message_at or datetime.min, c.id))
            if c.aftersales_status:
                s["aftersales_conv"] = True
                if c.aftersales_type:
                    s["conv_types"].append(c.aftersales_type)
    # AI 草稿意图（会话里最后一版 AI 草稿的全部意图）
    conv_ids = [cid for s in sigs.values() for _, cid in s["conv_ids"]]
    intents_of: dict[int, list[str]] = {}
    for i in range(0, len(conv_ids), 500):
        for cid, intent, intents in db.execute(
                select(ReplyJob.conversation_id, Draft.intent, Draft.intents).join(Draft, Draft.job_id == ReplyJob.id)
                .where(ReplyJob.conversation_id.in_(conv_ids[i:i + 500]), Draft.author == "agent").order_by(Draft.id)):
            intents_of[cid] = list(dict.fromkeys([x for x in [intent, *(intents or [])] if x]))
    for oid, s in sigs.items():
        s["order_at"], s["return_from"] = orders[oid].created_at, return_from
        ordered = [cid for _, cid in sorted(s["conv_ids"], reverse=True)]
        s["conv_ids"] = list(dict.fromkeys(([s["primary_conv"]] if s["primary_conv"] else []) + ordered))
        s["primary_conv"] = s["conv_ids"][0] if s["conv_ids"] else None
        for cid in s["conv_ids"]:
            for i in intents_of.get(cid, []):
                if i not in s["intents"]:
                    s["intents"].append(i)
    # 上线前补拉的历史会话（baseline_conversations，和工作台会话不是同一套 ID）：按买家 + 刊登对订单，对不上唯一订单的不接
    for oid, bids in baseline_links(db, store_id, orders).items():
        if oid in sigs:
            sigs[oid]["baseline_conv_ids"] = bids
    # 只有关联会话、没有任何售后信号的订单不进表（会话只是挂在订单上）
    keep = {oid: s for oid, s in sigs.items()
            if s["events"] or s["refunded"] or s["parcel"] or s["tickets"] or s["feishu"] or s["aftersales_conv"]}
    return orders, keep


def split_skus(text: str | None) -> list[str]:
    """订单 SKU 拆开：逗号（多件商品）和“+”（组合 SKU，如 BMW-30046+BMW-30047）都拆，按 SKU 统计时各算一次。"""
    return list(dict.fromkeys(x.strip() for x in re.split(r"[,+]", text or "") if x.strip()))


_ORDER_NO = re.compile(r"\b\d{2}-\d{5}-\d{5}\b")


def baseline_links(db: Session, store_id: str, orders: dict[str, EbayOrder]) -> dict[str, list[int]]:
    """上线前补拉的历史会话 → 订单（只读本地库）：消息里写了这个店铺的订单号就按订单号；否则同买家（不分大小写），
    会话刊登在订单商品里；买家在店铺只有一张订单、会话没关联刊登时也算。对上不止一张订单的不接。"""
    from sqlalchemy import inspect

    from .models import BaselineConversation, BaselineMessage

    if not orders or not inspect(db.get_bind()).has_table("baseline_conversations"):
        return {}
    by_buyer: dict[str, list[str]] = defaultdict(list)
    for oid, o in orders.items():
        if o.buyer:
            by_buyer[o.buyer.lower()].append(oid)
    texts: dict[int, str] = defaultdict(str)
    for cid, body in db.execute(select(BaselineMessage.conversation_id, BaselineMessage.body)
                                .where(BaselineMessage.store_id == store_id)):
        texts[cid] += " " + (body or "")
    out: dict[str, list[int]] = defaultdict(list)
    for c in db.scalars(select(BaselineConversation).where(BaselineConversation.store_id == store_id)):
        named = {x for x in _ORDER_NO.findall(texts.get(c.id, "")) if x in orders}
        mine = by_buyer.get((c.buyer_key or "").lower(), [])
        if named:
            hit = sorted(named)
        else:
            hit = [oid for oid in mine if c.item_id and c.item_id in (orders[oid].item_ids or orders[oid].item_id or "").split(",")]
            if not hit and not c.item_id and len(mine) == 1:
                hit = mine
        if len(hit) == 1:
            out[hit[0]].append(c.id)
    return dict(out)


def _skus(o: EbayOrder) -> str | None:
    parts = [x.strip() for x in (o.skus or "").split(",") if x.strip()]
    return "+".join(dict.fromkeys(parts))[:500] or None


def refresh_store(store_id: str, now: datetime | None = None) -> dict:
    """重算一个店铺：进表范围内的订单逐张写入 / 更新（幂等）；人工标记不动。"""
    now = now or utcnow()
    out = Counter()
    with session_scope() as db:
        orders, sigs = collect_store(db, store_id, now)
        if not sigs:
            return dict(out)
        store = db.get(Store, store_id)
        conv_mk = dict(db.execute(select(Conversation.id, Conversation.marketplace_id).where(
            Conversation.id.in_([s["primary_conv"] for s in sigs.values() if s["primary_conv"]]))).all())
        rows = {r.order_id: r for r in db.scalars(select(OrderAftersalesReason).where(
            OrderAftersalesReason.store_id == store_id, OrderAftersalesReason.order_id.in_(list(sigs))))}
        for oid, s in sigs.items():
            o = orders[oid]
            code, note, basis = classify(s)
            r = rows.get(oid)
            if r is None:
                r = OrderAftersalesReason(store_id=store_id, order_id=oid, created_at=now)
                db.add(r)
                out["created"] += 1
            before = (r.ai_reason, r.ai_note, r.ai_basis, r.refunded, r.events, r.conversation_ids, r.baseline_conversation_ids)
            r.conversation_id, r.conversation_ids = s["primary_conv"], s["conv_ids"] or None
            r.buyer_key = (o.buyer or "").lower() or None
            mk = conv_mk.get(s["primary_conv"]) if s["primary_conv"] else None
            r.marketplace_id = mk if mk and mk != "UNKNOWN" else (store.default_marketplace_id if store else None)
            r.skus, r.item_ids = _skus(o), (o.item_ids or o.item_id)
            r.order_created_at, r.order_total, r.currency = o.created_at, o.total, o.currency
            r.refunded = s["refunded"]
            r.refund_amount = round(s["refund_amount"], 2) if s["refund_amount"] else None
            r.refund_currency = s["refund_currency"] or (o.currency if s["refunded"] else None)
            r.refund_at = s["refund_at"]
            r.events = sorted(s["events"], key=lambda e: e["at"] or "") or None
            r.ai_reason, r.ai_note, r.ai_basis = code, (note or None) and note[:300], basis or None
            r.ai_at = now
            apply_final(r)
            r.baseline_conversation_ids = s["baseline_conv_ids"] or None
            if (r.ai_reason, r.ai_note, r.ai_basis, r.refunded, r.events, r.conversation_ids, r.baseline_conversation_ids) != before:
                r.updated_at = now
                out["updated"] += 1
        out["orders"] = len(orders)
        out["signal_orders"] = len(sigs)
    return dict(out)


def store_ids_with_orders() -> list[str]:
    with session_scope() as db:
        return [s for (s,) in db.execute(select(EbayOrder.store_id).distinct())]


def rebuild_all(now: datetime | None = None) -> dict:
    """全量重算全部有订单数据的店铺（人工标记不动）。"""
    total = Counter()
    errors = {}
    for sid in store_ids_with_orders():
        try:
            total.update(refresh_store(sid, now))
        except Exception as e:  # noqa: BLE001  一个店铺失败不影响其他
            log.exception("售后原因分类：店铺 %s 失败", sid)
            errors[sid] = f"{type(e).__name__}: {str(e)[:200]}"
    return {**dict(total), "errors": errors}


# ---------------------------------------------------------------- 统计
UNCLASSIFIED_KINDS = {"missing_return_data": "缺退货数据", "direct_refund": "直接退款", "other": "其他"}


def return_coverage(db: Session) -> dict[str, datetime]:
    """每个店铺库里最早的退货单时间（退货同步覆盖到哪）。"""
    return dict(db.execute(select(EbayCase.store_id, func.min(EbayCase.opened_at)).where(EbayCase.kind == "RETURN")
                           .group_by(EbayCase.store_id)).all())


def unclassified_kind(r: OrderAftersalesReason, return_from: datetime | None) -> str | None:
    """未分类再分：缺退货数据（退款早于退货同步范围，或有退货 / 退货升级的纠纷但拿不到退货原因）/ 直接退款（退货同步
    范围内只有退款、没有退货单）/ 其他（欺诈类付款纠纷、取消原因归不了类、只有售后标记等）。已分类的返回 None。"""
    if r.final_reason:
        return None
    ev = r.events or []
    kinds = {e.get("kind") for e in ev}
    if "RETURN" in kinds or any(e.get("kind") == "CASE" and e.get("reason") == "RETURN" for e in ev):
        return "missing_return_data"
    if r.refunded and kinds <= {"REFUND"}:
        at = r.order_created_at
        return "missing_return_data" if return_from is None or (at is not None and at < return_from) else "direct_refund"
    return "other"


def stats(db: Session, stores: list[str] | None, lo: datetime, hi: datetime, min_orders: int = 5,
          sku_sort: str = "refund_rate", sku_limit: int = 200) -> dict:
    """退款率：分母 = 范围内全部订单（ebay_orders，按下单时间），分子 = 其中有退款的（表里 refunded，或订单标记退款）。
    按原因用最终原因（人工 > AI），没有的算“未分类”；按 SKU 把逗号和“+”拆开（split_skus），一张订单的每个 SKU 各算一次。"""
    oq = select(EbayOrder.store_id, EbayOrder.order_id, EbayOrder.skus, EbayOrder.refunded).where(
        EbayOrder.created_at >= lo, EbayOrder.created_at < hi)
    rq = select(OrderAftersalesReason).where(OrderAftersalesReason.order_created_at >= lo,
                                             OrderAftersalesReason.order_created_at < hi)
    if stores is not None:
        oq = oq.where(EbayOrder.store_id.in_(stores))
        rq = rq.where(OrderAftersalesReason.store_id.in_(stores))
    rows = {(r.store_id, r.order_id): r for r in db.scalars(rq)}
    names = dict(db.execute(select(Store.id, Store.name)).all())
    total = refunded = 0
    by_store = defaultdict(lambda: {"orders": 0, "refunded": 0, "amount": Counter()})
    by_sku = defaultdict(lambda: {"orders": 0, "refunded": 0, "reasons": Counter()})
    by_reason = defaultdict(lambda: {"refunded": 0, "orders": 0, "amount": Counter()})
    amount = Counter()
    for store, oid, skus, o_refunded in db.execute(oq):
        r = rows.get((store, oid))
        is_ref = bool(o_refunded or (r is not None and r.refunded))
        total += 1
        refunded += is_ref
        st = by_store[store]
        st["orders"] += 1
        st["refunded"] += is_ref
        reason = (r.final_reason if r is not None else None) or "UNCLASSIFIED"
        if r is not None:
            by_reason[reason]["orders"] += 1
        if is_ref:
            by_reason[reason]["refunded"] += 1
            if r is not None and r.refund_amount:
                cur = r.refund_currency or "?"
                amount[cur] += r.refund_amount
                st["amount"][cur] += r.refund_amount
                by_reason[reason]["amount"][cur] += r.refund_amount
        for sku in split_skus(skus):
            k = by_sku[sku]
            k["orders"] += 1
            k["refunded"] += is_ref
            if is_ref:
                k["reasons"][reason] += 1

    def rate(n, d):
        return round(n / d, 4) if d else None

    def money(c: Counter) -> list[dict]:
        return [{"currency": k, "amount": round(v, 2)} for k, v in sorted(c.items(), key=lambda x: -x[1])]

    def label(code: str) -> str:
        return REASONS.get(code, "未分类")

    skus = []
    for sku, k in by_sku.items():
        main = k["reasons"].most_common(1)
        skus.append({"sku": sku, "orders": k["orders"], "refunded": k["refunded"], "refund_rate": rate(k["refunded"], k["orders"]),
                     "main_reason": main[0][0] if main else None, "main_reason_label": label(main[0][0]) if main else None,
                     "small_sample": k["orders"] < min_orders})
    key = {"orders": lambda x: (-x["orders"], x["sku"]), "refunded": lambda x: (-x["refunded"], -x["orders"], x["sku"])} \
        .get(sku_sort, lambda x: (x["small_sample"], -(x["refund_rate"] or 0), -x["refunded"], x["sku"]))
    skus.sort(key=key)
    unclassified = sum(1 for r in rows.values() if r.status == "UNCLASSIFIED")
    cover = return_coverage(db)
    sub = {k: {"label": v, "orders": 0, "refunded": 0} for k, v in UNCLASSIFIED_KINDS.items()}
    for r in rows.values():
        k = unclassified_kind(r, cover.get(r.store_id))
        if k:
            sub[k]["orders"] += 1
            sub[k]["refunded"] += bool(r.refunded)
    return {
        "period": {"from": lo.isoformat(timespec="seconds") + "Z", "to": hi.isoformat(timespec="seconds") + "Z"},
        "overall": {"orders": total, "refunded": refunded, "refund_rate": rate(refunded, total), "refund_amount": money(amount),
                    "aftersales_orders": len(rows)},
        "by_reason": [{"reason": code, "label": label(code), "aftersales_orders": v["orders"], "refunded": v["refunded"],
                       "share": rate(v["refunded"], refunded), "refund_rate": rate(v["refunded"], total),
                       "refund_amount": money(v["amount"])}
                      for code, v in sorted(by_reason.items(), key=lambda x: (-x[1]["refunded"], x[0]))],
        "by_sku": skus[:sku_limit], "sku_count": len(skus), "min_orders": min_orders,
        "by_store": [{"store_id": s, "store_name": names.get(s, s), "orders": v["orders"], "refunded": v["refunded"],
                      "refund_rate": rate(v["refunded"], v["orders"]), "refund_amount": money(v["amount"])}
                     for s, v in sorted(by_store.items(), key=lambda x: -x[1]["orders"])],
        "unclassified": {"orders": unclassified,
                         "refunded": sum(1 for r in rows.values() if r.status == "UNCLASSIFIED" and r.refunded),
                         **sub},
    }
