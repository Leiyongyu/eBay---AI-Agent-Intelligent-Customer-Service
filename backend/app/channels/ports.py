"""平台端口（Port）：Agent、知识库、审核、度量层只依赖这里的接口，不直接调 eBay；eBay 是第一个适配器（channels/ebay/），
演示 / ERP 数据源（sources/fixture.py、sources/mysql.py）和本地模拟发送（channels/mock.py）是另外的实现。
以后接 Amazon / Walmart：实现同样的端口放在 channels/<平台>/，在 channels/registry.py 里按店铺选，其他层不用改。

已收口：消息发送（MessagePort）、订单 / 刊登 / 包裹 / 售后状态的只读查询（OrderPort）。
售后写操作（退货、纠纷、咨询、取消、退款，case_actions_common / return_actions 等）还直接调 eBay Post-Order，随后再收口。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session


# ---------------------------------------------------------------- 订单 / 刊登 / 包裹 / 售后（只读）
class SourceNotAvailable(RuntimeError):
    """数据源未配置或暂不可用。工具层把它转成 ERROR，不能伪装成“没有数据”。"""


class OrderPort(ABC):
    """只读业务数据端口（原 BusinessSource）。所有查询都必须带 store_id，由调用方（ToolExecutor）从服务端上下文注入。"""

    name: str = "unknown"

    @abstractmethod
    def updated_at(self) -> str | None: ...

    @abstractmethod
    def get_listing(self, store_id: str, item_id: str) -> dict | None: ...

    @abstractmethod
    def find_products(self, store_id: str, sku: str) -> list[dict]: ...

    @abstractmethod
    def find_orders(self, store_id: str, buyer_key: str) -> list[dict]: ...

    @abstractmethod
    def find_packages(self, store_id: str, order_id: str) -> list[dict]: ...

    @abstractmethod
    def get_inventory(self, store_id: str, sku: str) -> dict | None: ...

    @abstractmethod
    def find_cases(self, store_id: str, order_ids: list[str]) -> list[dict]: ...


BusinessSource = OrderPort  # 旧名字，工具层和测试在用


# ---------------------------------------------------------------- 消息发送
class ChannelNotConfigured(RuntimeError):
    """请求根本没有发出：可以明确判定“未发送”。"""


class ChannelUncertain(RuntimeError):
    """请求已发出但结果不确定（超时、连接中断）：必须标记不确定并核对，不能直接重试。"""


@dataclass
class SendReceipt:
    platform_message_id: str
    http_status: str


@runtime_checkable
class MessagePort(Protocol):
    """给买家发一条消息（发件箱到期后调用）。不能发 → ChannelNotConfigured；结果不确定 → ChannelUncertain。"""

    name: str

    def send(self, db: Session, conv, text: str, media: list[dict] | None = None) -> SendReceipt: ...
