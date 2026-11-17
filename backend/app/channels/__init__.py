"""平台抽象：端口（ports）+ 适配器（ebay/、mock）+ 按店铺选适配器（registry）。见 ports.py 的说明。"""
from .ports import (BusinessSource, ChannelNotConfigured, ChannelUncertain, MessagePort, OrderPort, SendReceipt,
                    SourceNotAvailable)
from .registry import get_channel, get_message_port, get_order_port

__all__ = ["BusinessSource", "ChannelNotConfigured", "ChannelUncertain", "MessagePort", "OrderPort", "SendReceipt",
           "SourceNotAvailable", "get_channel", "get_message_port", "get_order_port"]
