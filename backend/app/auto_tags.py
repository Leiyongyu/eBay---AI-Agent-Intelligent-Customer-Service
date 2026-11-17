"""按 AI 识别的买家问题类型自动给会话打标记（和手动标记是同一套：可筛选、可手动去掉）。

- 意图按问题大类对应到现有标记，如 fitment → A待确认适配、shipping_status / delivery_delay → 查物流；对应不上的不打；
- 只打已经存在、启用着的标记项，不自动新建（标记项由客服在工作台维护）；
- 客服在某个会话上手动去掉过的自动标记，这个会话以后不再自动打这个标记。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .models import AuditEvent, Conversation, ConversationMark, MessageTag

ACTOR = "AI（自动标记）"
# 意图 → 标记名（必须是工作台里已有的标记项）
INTENT_TAGS: dict[str, str] = {
    "fitment": "A待确认适配",
    "handling_time": "查物流", "shipping_status": "查物流", "delivery_delay": "查物流",
    "shipping_cost": "查物流", "customs_duty": "查物流",
    "price_offer": "议价",
    "invoice_vat": "要发票",
    "address_change": "地址问题",
    "return_refund": "退货", "refund_status": "退货",
    "damaged_wrong": "产品问题", "quality_issue": "产品问题",
}


def _tag(db: Session, name: str) -> MessageTag | None:
    """已有且启用着的标记项；没有就不打（不自动新建）。"""
    tag = db.scalar(select(MessageTag).where(MessageTag.name == name))
    return tag if tag is not None and tag.is_active else None


def apply(db: Session, conv: Conversation, intents: list[str]) -> list[str]:
    """给会话加上这些意图对应的标记，返回新加的标记名。"""
    if not get_settings().auto_tag_enabled or conv is None:
        return []
    wanted = list(dict.fromkeys(INTENT_TAGS[i] for i in intents or [] if i in INTENT_TAGS))
    return apply_names(db, conv, wanted, {"intents": intents})


def apply_names(db: Session, conv: Conversation, wanted: list[str], why: dict | None = None) -> list[str]:
    """按标记名打标记，规则同 apply：开关、只打已有的标记项、手动去掉过的不再打。"""
    if not get_settings().auto_tag_enabled or conv is None or not wanted:
        return []
    removed = {(d or {}).get("tag_id") for d in db.scalars(select(AuditEvent.detail).where(
        AuditEvent.action == "CONVERSATION_UNMARKED", AuditEvent.conversation_id == conv.id))}
    have = set(db.scalars(select(ConversationMark.tag_id).where(ConversationMark.conversation_id == conv.id)))
    added = []
    for name in wanted:
        tag = _tag(db, name)
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
