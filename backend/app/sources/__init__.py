"""现有业务数据源的只读适配（第 12.2 节）。工具层只依赖 BusinessSource 接口。"""
from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import BusinessSource, SourceNotAvailable


@lru_cache
def get_business_source() -> BusinessSource:
    kind = get_settings().business_source
    if kind == "fixture":
        from .fixture import FixtureSource

        return FixtureSource()
    if kind == "mysql":
        from .mysql import MySQLSource

        return MySQLSource()
    raise SourceNotAvailable(f"BUSINESS_SOURCE={kind} 不支持")


@lru_cache
def get_ebay_source() -> BusinessSource:
    from .ebay import EbaySource

    return EbaySource()


def get_source_for_store(store_id: str) -> BusinessSource:
    """按店铺选数据源：对接了 eBay 的店铺（凭证表或 .env 单店铺）实时查该店自己的 eBay 数据；
    其余店铺（演示店铺等）用 BUSINESS_SOURCE 配置的数据源。"""
    from ..ebay.stores import is_ebay_store

    return get_ebay_source() if is_ebay_store(store_id) else get_business_source()


__all__ = ["BusinessSource", "SourceNotAvailable", "get_business_source", "get_ebay_source", "get_source_for_store"]
