"""本地模拟发送（MessagePort 的一个实现）：EBAY_CHANNEL=mock、没对接的店铺、测试会话只在本地记一条，不连任何平台。"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models import Conversation
from .ports import SendReceipt


class MockChannel:
    name = "mock"

    def __init__(self, prefix: str):
        self.prefix = prefix

    def send(self, db: Session, conv: Conversation, text: str, media: list[dict] | None = None) -> SendReceipt:
        return SendReceipt(platform_message_id=f"{self.prefix}{uuid.uuid4().hex[:12]}", http_status="local")
