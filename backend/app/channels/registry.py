"""按店铺 / 会话选适配器：消息发送选 MessagePort，订单等只读查询选 OrderPort。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Conversation, Store
from .ports import MessagePort, OrderPort


def get_channel(db: Session | None = None, conv: Conversation | None = None, prefix: str = "SIM-OUT-") -> MessagePort:
    """按会话选渠道：对接了 eBay 的店铺、来自 eBay 的会话走真实发送，其余只记本地。"""
    from .ebay.messages import EbayChannel, is_live_store
    from .mock import MockChannel

    if get_settings().ebay_channel == "ebay" and db is not None and conv is not None and conv.channel_source == "ebay":
        if is_live_store(db.get(Store, conv.store_id)):
            return EbayChannel()
    return MockChannel(prefix)


get_message_port = get_channel


def get_order_port(store_id: str) -> OrderPort:
    """按店铺选只读数据端口：对接了 eBay 的店铺实时查 eBay，其余用 BUSINESS_SOURCE 配置的数据源（演示 / ERP 库）。"""
    from .. import sources

    return sources.get_source_for_store(store_id)
