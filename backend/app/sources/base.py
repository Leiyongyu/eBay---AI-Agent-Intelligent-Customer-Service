from __future__ import annotations

from abc import ABC, abstractmethod


class SourceNotAvailable(RuntimeError):
    """数据源未配置或暂不可用。工具层把它转成 ERROR，不能伪装成“没有数据”。"""


class BusinessSource(ABC):
    """只读业务数据源。所有查询都必须带 store_id，由调用方（ToolExecutor）从服务端上下文注入。"""

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
