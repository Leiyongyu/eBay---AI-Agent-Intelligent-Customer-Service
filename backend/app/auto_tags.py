"""按 AI 识别的买家问题类型自动给会话打标记（和手动标记是同一套：可筛选、可手动去掉）。

- 意图按问题大类对应到标记，如 fitment → 适配咨询、shipping_status / delivery_delay → 物流查询；致谢、无法归类的不打；
- 标记项不存在就自动建（创建人记为 AI）；但有人删掉过的标记项不再自动建回来；
- 客服在某个会话上手动去掉过的自动标记，这个会话以后不再自动打这个标记。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import AuditEvent, Conversation, ConversationMark, MessageTag

ACTOR = "AI（自动标记）"
# 意图 → (标记名, 颜色)
INTENT_TAGS: dict[str, tuple[str, str]] = {
    "fitment": ("适配咨询", "primary"),
    "item_spec": ("商品咨询", "info"), "stock": ("商品咨询", "info"),
    "handling_time": ("物流查询", "primary"), "shipping_status": ("物流查询", "primary"),
    "delivery_delay": ("物流查询", "primary"), "shipping_cost": ("物流查询", "primary"),
    "customs_duty": ("物流查询", "primary"),
    "price_offer": ("议价", "warning"),
    "invoice_vat": ("发票", "success"),
    "order_how_to": ("下单咨询", "info"),
    "address_change": ("改地址", "warning"),
    "cancel_request": ("取消订单", "danger"),
    "return_refund": ("退货退款", "danger"), "refund_status": ("退货退款", "danger"),
    "damaged_wrong": ("破损错发", "danger"),
    "feedback": ("评价", "warning"),
    "off_platform": ("风险来信", "danger"), "abuse_fraud_legal": ("风险来信", "danger"),
}


def _tag(db: Session, name: str, color: str) -> MessageTag | None:
    tag = db.scalar(select(MessageTag).where(MessageTag.name == name))
    if tag is not None:
        return tag if tag.is_active else None
    deleted = db.scalars(select(AuditEvent.detail).where(AuditEvent.action == "MESSAGE_TAG_DELETED")).all()
    if any((d or {}).get("tag") == name for d in deleted):
        return None  # 有人删掉过这个标记项：不再自动建回来
    top = db.scalar(select(func.max(MessageTag.sort))) or 0
    tag = MessageTag(name=name, color=color, sort=top + 10, created_by=ACTOR)
    db.add(tag)
    db.flush()
    return tag


def apply(db: Session, conv: Conversation, intents: list[str]) -> list[str]:
    """给会话加上这些意图对应的标记，返回新加的标记名。"""
    if not get_settings().auto_tag_enabled or conv is None:
        return []
    wanted = list(dict.fromkeys(INTENT_TAGS[i] for i in intents or [] if i in INTENT_TAGS))
    return apply_names(db, conv, wanted, {"intents": intents})


def apply_names(db: Session, conv: Conversation, wanted: list[tuple[str, str]], why: dict | None = None) -> list[str]:
    """按标记名（和颜色）打标记，规则同 apply：开关、手动去掉过的不再打、删掉过的标记项不再建。"""
    if not get_settings().auto_tag_enabled or conv is None or not wanted:
        return []
    removed = {(d or {}).get("tag_id") for d in db.scalars(select(AuditEvent.detail).where(
        AuditEvent.action == "CONVERSATION_UNMARKED", AuditEvent.conversation_id == conv.id))}
    have = set(db.scalars(select(ConversationMark.tag_id).where(ConversationMark.conversation_id == conv.id)))
    added = []
    for name, color in wanted:
        tag = _tag(db, name, color)
        if tag is None or tag.id in have or tag.id in removed or None in removed:
            continue  # 已有，或客服在这个会话上手动去掉过
        db.add(ConversationMark(conversation_id=conv.id, store_id=conv.store_id, tag_id=tag.id,
                                marked_by=ACTOR, marked_at=utcnow()))
        have.add(tag.id)
        added.append(name)
    if added:
        db.add(AuditEvent(actor=ACTOR, action="CONVERSATION_AUTO_TAGGED", store_id=conv.store_id,
                          conversation_id=conv.id, detail={"tags": added, **(why or {})}))
        db.flush()
    return added
