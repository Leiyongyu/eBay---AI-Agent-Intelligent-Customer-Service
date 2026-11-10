"""异常包裹跟进表的自动登记与补写：异常一出现就自动建一条，之后按进度补写，提示人工下一步该做什么。

什么时候自动建（每张订单只建一条跟进中的记录，后面的进展都更新到这条上）：
- 回复流程的物流规则：断更、无轨迹、妥投未收到（买家说没收到）、退回 / 丢失 / 损坏、取件点超时、空包裹；
- 提交了谷仓查件单；
- 拉消息的店铺里，买家在 eBay 开了未收到货咨询 / 纠纷，或因到货损坏、发错、缺件开了退货。
之后自动补写：查件单号、谷仓回复结论、退款（处理方式、金额）、纠纷关闭 → 标“可以完结”。只补空白字段，人工填过的不覆盖；
“下一步”和“可以完结”由系统按进度更新。不自动完结，最后由人确认。

负责人：会话由谁在处理就归谁（认领人，其次最近人工回复的人）；会话没人处理归负责这个店铺的客服，店铺没分配人的归管理员。
同一订单的记录完结后，触发时间早于完结时间的不再重建（避免每小时同步又建回来）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import (
    AuditEvent,
    Conversation,
    EbayCase,
    EbayOrderLine,
    LogisticsTicket,
    OutboundMessage,
    ParcelFollowup,
    Role,
    User,
    UserStore,
    WorkClaim,
)

log = logging.getLogger(__name__)
ACTOR = "系统（自动登记）"
REOPEN_GAP = timedelta(days=3)  # 记录完结后 3 天内，同一订单的物流异常不再重建

# 回复流程的物流原因 → 原因类型
REASON_OF = {
    "TRACKING_STALLED": "断更", "TRACKING_RESHIP_REVIEW": "断更", "TRACKING_NOT_FOUND": "无轨迹",
    "TRACKING_DELIVERED_NOT_RECEIVED": "妥投未收到", "TRACKING_LOST_DAMAGED": "物流异常退回",
    "TRACKING_PICKUP_OVERDUE": "物流异常退回", "TRACKING_EXCEPTION": "物流异常退回", "EMPTY_PARCEL": "到货损坏",
}
# 退货原因 → 原因类型（只登记物流 / 发货问题；质量问题走飞书售后群）
RETURN_REASON = {"ARRIVED_DAMAGED": "到货损坏", "WRONG_ITEM": "谷仓发错", "DIFFERENT_FROM_LISTING": "谷仓发错",
                 "MISSING_PARTS": "谷仓发错"}
TICKET_REASON = {"断更": "断更", "无Ascan": "无轨迹", "妥投未收到": "妥投未收到", "申请POD": "妥投未收到",
                 "轨迹异常": "物流异常退回"}
DISPUTE_KINDS = ("INQUIRY", "CASE")


def _md(d: datetime | None = None) -> str:
    d = d or datetime.now()
    return f"{d.month}.{d.day}"


# ------------------------------------------------------------------ 负责人
def owner_for(db: Session, store_id: str | None, conv_id: int | None) -> tuple[int, str] | None:
    def by_name(name: str | None):
        if not name:
            return None
        u = db.scalar(select(User).where(User.display_name == name, User.is_active.is_(True)).limit(1))
        return (u.id, u.display_name) if u else None

    if conv_id:
        claim = db.scalar(select(WorkClaim).where(WorkClaim.conversation_id == conv_id).order_by(WorkClaim.claimed_at.desc()).limit(1))
        hit = by_name(claim.claimed_by if claim else None)
        if hit:
            return hit
        ob = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_id == conv_id, OutboundMessage.source == "manual")
                       .order_by(OutboundMessage.id.desc()).limit(1))
        hit = by_name(ob.actor if ob else None)
        if hit:
            return hit
    active = select(User).where(User.is_active.is_(True))
    if store_id:
        u = db.scalar(active.join(UserStore, UserStore.user_id == User.id)
                      .where(UserStore.store_id == store_id, User.role != Role.ADMIN).order_by(User.id).limit(1))
        u = u or db.scalar(active.where(User.all_stores.is_(True), User.role != Role.ADMIN).order_by(User.id).limit(1))
        if u:
            return u.id, u.display_name
    u = db.scalar(active.where(User.role == Role.ADMIN).order_by(User.id).limit(1))
    return (u.id, u.display_name) if u else None


# ------------------------------------------------------------------ 登记 / 补写
def upsert(db: Session, *, store_id: str, order_id: str | None, reason: str, conv_id: int | None = None,
           buyer: str | None = None, fields: dict | None = None, note: str | None = None, next_step: str | None = None,
           next_follow_on: str | None = None, can_close: bool | None = None, create: bool = True,
           since: datetime | None = None) -> ParcelFollowup | None:
    """这张订单有跟进中的记录就补写，没有就新建（create=False 时只补写）。fields 只补空白字段。
    since：触发时间——这张订单的记录在它之后完结过，就不重建。"""
    if not order_id or not store_id:
        return None
    row = db.scalar(select(ParcelFollowup).where(ParcelFollowup.store_id == store_id, ParcelFollowup.order_id == order_id,
                                                 ParcelFollowup.status == "OPEN").order_by(ParcelFollowup.id.desc()).limit(1))
    created = False
    if row is None:
        if not create:
            return None
        cutoff = since or (utcnow() - REOPEN_GAP)
        if db.scalar(select(ParcelFollowup.id).where(ParcelFollowup.store_id == store_id, ParcelFollowup.order_id == order_id,
                                                     ParcelFollowup.status == "DONE", ParcelFollowup.closed_at >= cutoff).limit(1)):
            return None  # 同一单刚处理完：不重建
        owner = owner_for(db, store_id, conv_id)
        if owner is None:
            return None
        row = ParcelFollowup(owner_id=owner[0], owner_name=owner[1][:64], origin="AUTO", status="OPEN", reason=reason,
                             occurred_on=datetime.now().date().isoformat(), store_id=store_id, order_id=order_id)
        db.add(row)
        created = True
    row.conversation_id = row.conversation_id or conv_id
    row.buyer_key = row.buyer_key or buyer
    for k, v in (fields or {}).items():
        if v not in (None, "") and getattr(row, k) in (None, ""):
            setattr(row, k, v)
    if note:
        old = row.reason_note or ""
        if note not in old:
            row.reason_note = (f"{old}；{note}" if old else note)[:300]
    if next_step:
        row.next_step = next_step[:300]
    if next_follow_on and (not row.next_follow_on or next_follow_on < row.next_follow_on):
        row.next_follow_on = next_follow_on
    if can_close is not None:
        row.can_close = can_close
    db.flush()
    if created:
        db.add(AuditEvent(actor=ACTOR, action="PARCEL_FOLLOWUP_AUTO_CREATED", store_id=store_id, conversation_id=conv_id,
                          detail={"id": row.id, "reason": reason, "order_id": order_id, "owner": row.owner_name}))
    return row


def _in_days(n: int) -> str:
    return (datetime.now() + timedelta(days=n)).date().isoformat()


# ------------------------------------------------------------------ 回复流程（物流规则）
def _packages(state) -> list[dict]:
    rows = []
    for o in state.get("observations") or []:
        if o.get("tool_name") == "get_package_status" and isinstance(o.get("data"), (dict, list)):
            data = o["data"] if isinstance(o["data"], list) else [o["data"]]
            rows += [r for r in data if isinstance(r, dict)]
    return rows


def _order(state) -> dict:
    return next((o["data"] for o in state.get("observations") or [] if o.get("tool_name") == "get_authorized_order_context"
                 and o.get("status") == "FOUND" and isinstance(o.get("data"), dict)), {})


def from_reply(db: Session, conv: Conversation | None, state, reasons: list[str], follow: dict | None) -> list[int]:
    """回复流程落库时：物流需要人工的原因，登记到跟进表。返回登记 / 补写的记录 ID。"""
    hits = [r for r in reasons if r in REASON_OF]
    if conv is None or not hits or conv.conversation_type == "FROM_EBAY":
        return []
    order = _order(state)
    pkgs = _packages(state)
    pkg = next((p for p in pkgs if (p.get("assessment") or {}).get("needs_human")), None) or (pkgs[0] if pkgs else {})
    order_id = pkg.get("order_id") or order.get("order_id")
    reason = REASON_OF[hits[0]]
    sub = str(((pkg.get("tracking") or {}).get("sub_status")) or "")
    if hits[0] == "TRACKING_LOST_DAMAGED" and "Damage" in sub:
        reason = "到货损坏"
    a = pkg.get("assessment") or {}
    note = f"{_md()} {a.get('stage_label') or reason}" if a.get("stage_label") else f"{_md()} {reason}"
    tickets = (follow or {}).get("tickets") or []
    submitted = [t["io_code"] for t in tickets if t.get("submitted") and t.get("io_code")]
    drafted = [t for t in tickets if t.get("io_code") and not t.get("submitted")]
    if submitted:
        step, follow_on = "已提交谷仓查件单，等查件结果", _in_days(3)
    elif drafted:
        step, follow_on = "已起草谷仓查件单，待审核提交", _in_days(0)
    else:
        step, follow_on = "核实物流：查件 / 重发 / 退款", _in_days(0)
    sku = order.get("sku") or next((i.get("sku") for i in order.get("items") or [] if i.get("sku")), None)
    row = upsert(db, store_id=conv.store_id, order_id=order_id, reason=reason, conv_id=conv.id, buyer=conv.buyer_key,
                 fields={"sku": sku, "carrier": pkg.get("carrier"), "tracking_no": pkg.get("tracking_number"),
                         "ticket_no": ", ".join(submitted) or None},
                 note=note, next_step=step, next_follow_on=follow_on)
    return [row.id] if row else []


# ------------------------------------------------------------------ 谷仓查件单
def _ticket_conv(db: Session, t: LogisticsTicket) -> Conversation | None:
    return db.get(Conversation, t.conversation_id) if t.conversation_id else None


def on_ticket_submitted(db: Session, t: LogisticsTicket) -> ParcelFollowup | None:
    """查件单提交到谷仓：登记（或补写）查件单号，下一步“等查件结果”，3 天后跟进。"""
    conv = _ticket_conv(db, t)
    store_id = t.store_id or (conv.store_id if conv else None)
    reason = next((v for k, v in TICKET_REASON.items() if k in (t.type_name or "")), "其他")
    row = upsert(db, store_id=store_id, order_id=t.reference_no, reason=reason, conv_id=conv.id if conv else None,
                 buyer=t.buyer_key or (conv.buyer_key if conv else None),
                 fields={"tracking_no": t.tracking_no}, note=f"{_md()} 提交查件单（{t.type_name or '查件'}）",
                 next_step="已提交谷仓查件单，等查件结果", next_follow_on=_in_days(3))
    if row and t.io_code and t.io_code not in (row.ticket_no or ""):
        row.ticket_no = (f"{row.ticket_no}, {t.io_code}" if row.ticket_no else t.io_code)[:200]
    return row


def on_ticket_result(db: Session, t: LogisticsTicket, label: str, how: str) -> ParcelFollowup | None:
    """谷仓回复了查件单：处理结果写回复结论（空白时），下一步按规则写，今天跟进。"""
    conv = _ticket_conv(db, t)
    store_id = t.store_id or (conv.store_id if conv else None)
    reason = next((v for k, v in TICKET_REASON.items() if k in (t.type_name or "")), "其他")
    step = f"谷仓已回复：{label}" + (f"，{how}" if how and how != "自动回复" else "，已按结果回复买家（审核后发）")
    return upsert(db, store_id=store_id, order_id=t.reference_no, reason=reason, conv_id=conv.id if conv else None,
                  buyer=t.buyer_key or (conv.buyer_key if conv else None),
                  fields={"tracking_no": t.tracking_no, "result": f"谷仓回复：{label}"}, next_step=step,
                  next_follow_on=_in_days(0))


# ------------------------------------------------------------------ eBay 纠纷、退货、退款（每小时随售后同步）
def _line(db: Session, store_id: str, ref: str | None) -> EbayOrderLine | None:
    if not ref:
        return None
    return db.scalar(select(EbayOrderLine).where(EbayOrderLine.store_id == store_id,
                                                 (EbayOrderLine.line_key == ref) | (EbayOrderLine.order_id == ref)).limit(1))


def _case_conv(db: Session, store_id: str, buyer: str | None, order_id: str | None, item_id: str | None) -> int | None:
    from .aftersales_mail.service import order_conversation

    return order_conversation(db, store_id, buyer, order_id, [item_id] if item_id else None) if buyer else None


def from_cases(db: Session, store_id: str, now: datetime | None = None) -> dict:
    """一个店铺的售后单 → 跟进表：新开的未收到货咨询 / 纠纷、损坏 / 发错 / 缺件退货登记；已关闭、已退款的标可以完结。"""
    from .case_labels import reason_label, status_label

    now = now or utcnow()
    stats = {"created_or_updated": 0, "can_close": 0}
    recent = now - timedelta(days=60)
    cases = db.scalars(select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.hidden_at.is_(None),
                                              EbayCase.kind.in_((*DISPUTE_KINDS, "RETURN", "REFUND")),
                                              (EbayCase.opened_at >= recent) | (EbayCase.status_changed_at >= recent))).all()
    for c in cases:
        if (c.detail or {}).get("origin") == "BASELINE_BACKFILL":
            continue  # 上线前基线补拉的旧单（case_backfill.py）：只用于统计，不登记、不更新跟进
        line = _line(db, store_id, c.order_id)
        order_id = line.order_id if line else (c.order_id if c.kind in ("RETURN", "REFUND") else None)
        if not order_id:
            continue
        buyer = c.buyer or (line.buyer if line else None)
        label = {"INQUIRY": "未收到货咨询（INR）", "CASE": "纠纷", "RETURN": "退货", "REFUND": "退款"}[c.kind]
        fields = {"sku": line.sku if line else None}
        if c.kind == "REFUND":
            row = upsert(db, store_id=store_id, order_id=order_id, reason="其他", buyer=buyer, create=False,
                         fields={**fields, "action": "退款", "amount": abs(c.amount) if c.amount else None,
                                 "currency": c.currency, "result": "已退款"},
                         next_step="已退款，确认后可以完结", can_close=True)
            stats["can_close"] += bool(row)
            continue
        if c.kind == "RETURN" and c.reason not in RETURN_REASON:
            continue  # 其他退货（不想要、不适配、质量问题）不算异常包裹
        if not c.is_open:
            row = upsert(db, store_id=store_id, order_id=order_id, reason="其他", buyer=buyer, create=False,
                         note=f"{label}已关闭", next_step=f"{label}已关闭（{status_label(c.status) or c.status}），确认后可以完结",
                         can_close=True)
            stats["can_close"] += bool(row)
            continue
        reason = RETURN_REASON.get(c.reason) or "未收到货纠纷"
        due = c.due_at.date().isoformat() if c.due_at else None
        conv_id = _case_conv(db, store_id, buyer, order_id, c.item_id)
        what = f"买家开了{label}" + (f"：{reason_label(c.reason) or c.reason}" if c.kind == "RETURN" else "")
        step = what + (f"，{c.due_at.month}.{c.due_at.day} 前要处理" if c.due_at else "") + \
            ("；需核实是谷仓还是供应商发错" if reason == "谷仓发错" else "")
        row = upsert(db, store_id=store_id, order_id=order_id, reason=reason, conv_id=conv_id, buyer=buyer, fields=fields,
                     note=f"{_md(c.opened_at)} {what}（{c.external_id}）" if c.opened_at else what,
                     next_step=step, next_follow_on=due or _in_days(0), since=c.opened_at)
        stats["created_or_updated"] += bool(row)
    return stats


def run_cases_for_synced_stores(db: Session) -> dict:
    """只登记拉消息的店铺（EBAY_SYNC_STORES / EBAY_LIVE_STORES）：其他店铺没人在本系统处理。"""
    from .ebay.stores import ebay_store_targets

    out = {}
    for target in ebay_store_targets():
        try:
            with db.begin_nested():
                out[target.store_id] = from_cases(db, target.store_id)
        except Exception:  # noqa: BLE001  单个店铺失败不影响其他
            log.exception("异常包裹跟进：店铺 %s 的售后单登记失败", target.store_id)
    return out
