"""eBay 收发渠道适配。

EBAY_CHANNEL=mock：本地模拟渠道，“发送”= 在本地会话里追加一条店铺消息，用于开发联调，不连 eBay。
EBAY_CHANNEL=ebay：真实渠道，按方案 P0 先复用现有收发代码（Message API sendMessage），尚未接入。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import get_settings
from ..ingestion.service import _append_message
from ..models import Conversation


class ChannelNotConfigured(RuntimeError):
    """请求根本没有发出：可以明确判定“未发送”。"""


class ChannelUncertain(RuntimeError):
    """请求已发出但结果不确定（超时、连接中断）：必须标记 SEND_UNCERTAIN 并对账，不能直接重试。"""


@dataclass
class SendReceipt:
    platform_message_id: str | None
    http_status: str


class MockChannel:
    name = "mock"

    def send(self, db: Session, conv: Conversation, text: str) -> SendReceipt:
        mid = f"MOCK-OUT-{uuid.uuid4().hex[:12]}"
        _append_message(db, conv, mid, "SELLER", "OUT", text, None)
        conv.row_version += 1
        return SendReceipt(platform_message_id=mid, http_status="200 (mock)")


class EbayChannel:
    name = "ebay"

    def send(self, db: Session, conv: Conversation, text: str) -> SendReceipt:
        raise ChannelNotConfigured("真实 eBay 发送渠道尚未接入（P0：复用现有收发代码后实现）")


def get_channel():
    return EbayChannel() if get_settings().ebay_channel == "ebay" else MockChannel()
