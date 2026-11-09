"""eBay 收发渠道适配（只由发件箱 sending/outbox.py 在延迟到期后调用）。

EBAY_CHANNEL=ebay 且店铺在 EBAY_LIVE_STORES 里、会话来自 eBay：真实调用 Message API sendMessage；
其余（EBAY_CHANNEL=mock、没对接的店铺、测试会话）：只在本地记一条，不连 eBay。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Conversation, Store


class ChannelNotConfigured(RuntimeError):
    """请求根本没有发出：可以明确判定“未发送”。"""


class ChannelUncertain(RuntimeError):
    """请求已发出但结果不确定（超时、连接中断）：必须标记不确定并核对，不能直接重试。"""


@dataclass
class SendReceipt:
    platform_message_id: str
    http_status: str


def is_live_store(store: Store | None) -> bool:
    """店铺是否对接 eBay 真实收发（EBAY_LIVE_STORES 按店铺名或 ID 匹配，不区分大小写）。"""
    keys = get_settings().ebay_live_store_keys
    return bool(store) and bool(keys) and ((store.name or "").lower() in keys or store.id.lower() in keys)


class MockChannel:
    name = "mock"

    def __init__(self, prefix: str):
        self.prefix = prefix

    def send(self, db: Session, conv: Conversation, text: str, media: list[dict] | None = None) -> SendReceipt:
        return SendReceipt(platform_message_id=f"{self.prefix}{uuid.uuid4().hex[:12]}", http_status="local")


class EbayChannel:
    name = "ebay"

    def send(self, db: Session, conv: Conversation, text: str, media: list[dict] | None = None) -> SendReceipt:
        from ..ebay.auth import EbayAuthError, for_store
        from ..ebay.message_api import EbayApiError, EbaySendUncertain, MessageApiClient, send_message

        for m in media or []:  # 附件地址 eBay 要能打开：先自己从公网取一次，打不开就不发（转人工），免得买家收到坏图
            problem = check_public_url(m["mediaUrl"])
            if problem:
                raise ChannelNotConfigured(f"附件“{m['mediaName']}”的公网地址打不开（{problem}），没有发出；"
                                           "请确认 main.py 和 Cloudflare 隧道在运行")
        try:
            data = send_message(MessageApiClient(for_store(conv.store_id)), text=text,
                                conversation_id=conv.ebay_conversation_id,
                                other_party=None if conv.ebay_conversation_id else conv.buyer_key, item_id=conv.item_id,
                                media=media)
        except EbaySendUncertain as e:
            raise ChannelUncertain(str(e)) from None
        except (EbayApiError, EbayAuthError) as e:
            raise ChannelNotConfigured(str(e)) from None
        mid = str(data.get("messageId") or "")
        if not mid:
            raise ChannelUncertain("eBay 返回成功但没有消息 ID，请到 eBay 后台核对")
        return SendReceipt(platform_message_id=mid, http_status="201")


def check_public_url(url: str) -> str | None:
    """从公网取一次附件地址：能打开返回 None，否则返回原因。"""
    import httpx

    try:
        r = httpx.head(url, timeout=15, follow_redirects=True)
        if r.status_code == 405:
            r = httpx.get(url, timeout=15, follow_redirects=True)
    except httpx.HTTPError as e:
        return type(e).__name__
    return None if r.status_code == 200 else f"HTTP {r.status_code}"


def get_channel(db: Session | None = None, conv: Conversation | None = None, prefix: str = "SIM-OUT-"):
    """按会话选渠道：对接了 eBay 的店铺、来自 eBay 的会话走真实发送，其余只记本地。"""
    if get_settings().ebay_channel == "ebay" and db is not None and conv is not None and conv.channel_source == "ebay":
        if is_live_store(db.get(Store, conv.store_id)):
            return EbayChannel()
    return MockChannel(prefix)
