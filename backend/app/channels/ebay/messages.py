"""eBay 消息适配器（MessagePort）：EBAY_CHANNEL=ebay 且店铺在 EBAY_LIVE_STORES 里、会话来自 eBay 时，真实调用 Message API sendMessage。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import Conversation, Store
from ..ports import ChannelNotConfigured, ChannelUncertain, SendReceipt


def is_live_store(store: Store | None) -> bool:
    """店铺是否对接 eBay 真实收发（EBAY_LIVE_STORES 按店铺名或 ID 匹配，不区分大小写）。"""
    keys = get_settings().ebay_live_store_keys
    return bool(store) and bool(keys) and ((store.name or "").lower() in keys or store.id.lower() in keys)


class EbayChannel:
    name = "ebay"

    def send(self, db: Session, conv: Conversation, text: str, media: list[dict] | None = None) -> SendReceipt:
        from ...ebay.auth import EbayAuthError, for_store
        from ...ebay.message_api import EbayApiError, EbaySendUncertain, MessageApiClient, send_message

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


EbayMessageAdapter = EbayChannel


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


# 旧路径 app.sending.channel 是本模块的别名（测试和旧代码对它 monkeypatch，比如 check_public_url）：把旧名字也放进来
from ..mock import MockChannel  # noqa: E402,F401
from ..ports import MessagePort  # noqa: E402,F401
from ..registry import get_channel  # noqa: E402,F401
