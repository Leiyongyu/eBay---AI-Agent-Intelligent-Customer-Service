"""MemoryService：当前会话最近原文（第 11.3 节第 1 层）。

- 只读取 (store_id, conversation_id) 范围内的消息，不跨店铺。
- 客服侧只包含实际发出的消息（messages 表里 direction=OUT 的记录），候选草稿不会出现在记忆里。
- 本次任务覆盖的买家消息必须全部保留，不能被截断。
- 机器译文（zh_translation）不进入记忆。
第 2 层（较早摘要）与第 3 层（同店铺跨会话历史）尚未实现，见文档第 21 章。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..knowledge.retriever import estimate_tokens
from ..models import Message


def build_recent(db: Session, store_id: str, conversation_id: int, required_ids: set[str],
                 max_messages: int = 12, token_budget: int = 4000) -> dict:
    rows = db.scalars(
        select(Message)
        .where(Message.store_id == store_id, Message.conversation_id == conversation_id)
        .order_by(Message.conversation_seq)
    ).all()

    picked: list[Message] = []
    tokens = 0
    # 从最新往前取，必需消息无条件保留，其余受条数与 token 预算约束
    for m in reversed(rows):
        cost = estimate_tokens(m.body)
        required = m.ebay_message_id in required_ids
        if not required and (len(picked) >= max_messages or tokens + cost > token_budget):
            continue
        picked.append(m)
        tokens += cost
    picked.reverse()

    missing_required = required_ids - {m.ebay_message_id for m in picked}
    return {
        "messages": [
            {
                "id": m.ebay_message_id,
                "role": m.sender_role,
                "at": m.sent_at.isoformat(timespec="seconds") + "Z",
                "text": m.body,
                "in_task": m.ebay_message_id in required_ids,
            }
            for m in picked
        ],
        "total_messages": len(rows),
        "truncated": len(picked) < len(rows),
        "tokens": tokens,
        "missing_required": sorted(missing_required),
    }
