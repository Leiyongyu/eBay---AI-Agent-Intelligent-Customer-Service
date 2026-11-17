"""会话已读/未读：团队共享一份状态（每个客服负责自己的店铺，不按人分开记）。

- 已读位置 conversations.read_seq 是会话内顺序号；之后收到的消息（direction=IN：买家、eBay 系统、发送方未知）算未读。
- 工作台打开会话即标为已读；“标为未读”把位置退到最后一条收到的消息之前（未读 1 条）。
- 同步 eBay 侧已读（只读，不写回 eBay）：会话列表的 unreadCount 为 N 时，最后 N 条收到的消息未读、之前的算已读；
  店铺发出的消息之前的也算已读（在 eBay 后台回复过就是看过）。eBay 侧只会让已读位置前进，不会把这边已读的改回未读。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .models import Conversation, Message

EBAY_READER = "eBay后台"
INITIAL_READER = "初始化"


def unread_count_expr():
    """列表查询用的关联子查询：每个会话的未读条数。"""
    return (select(func.count(Message.id))
            .where(Message.conversation_id == Conversation.id, Message.direction == "IN",
                   Message.conversation_seq > func.coalesce(Conversation.read_seq, 0))
            .correlate(Conversation).scalar_subquery())


def unread_count(db: Session, conv: Conversation) -> int:
    return db.scalar(select(func.count(Message.id)).where(
        Message.conversation_id == conv.id, Message.direction == "IN",
        Message.conversation_seq > (conv.read_seq or 0))) or 0


def _max_seq(db: Session, conv_id: int, direction: str | None = None) -> int:
    q = select(func.max(Message.conversation_seq)).where(Message.conversation_id == conv_id)
    if direction:
        q = q.where(Message.direction == direction)
    return db.scalar(q) or 0


def _set(conv: Conversation, seq: int, by: str) -> None:
    conv.read_seq = seq
    conv.read_by = by[:64]
    conv.read_at = utcnow()


def mark_read(db: Session, conv: Conversation, by: str) -> bool:
    top = _max_seq(db, conv.id)
    if conv.read_seq is not None and conv.read_seq >= top:
        return False
    _set(conv, top, by)
    return True


def mark_unread(db: Session, conv: Conversation, by: str) -> bool:
    last_in = _max_seq(db, conv.id, "IN")
    if not last_in or (conv.read_seq or 0) < last_in:
        return False  # 没有收到的消息，或本来就未读
    _set(conv, last_in - 1, by)
    return True


def apply_platform_unread(db: Session, conv: Conversation, unread: int | None) -> bool:
    """按 eBay 会话列表的 unreadCount 推进已读位置；返回是否改动了已读位置。"""
    if unread is None:
        return False
    unread = max(int(unread), 0)
    if conv.platform_unread_count != unread:
        conv.platform_unread_count = unread
    if unread == 0:
        target = _max_seq(db, conv.id)
    else:  # 倒数第 N+1 条收到的消息及之前都已读
        target = db.scalar(select(Message.conversation_seq)
                           .where(Message.conversation_id == conv.id, Message.direction == "IN")
                           .order_by(Message.conversation_seq.desc()).offset(unread).limit(1)) or 0
    target = max(target, _max_seq(db, conv.id, "OUT"))
    if conv.read_seq is None or target > conv.read_seq:
        _set(conv, target, EBAY_READER)
        return True
    return False
