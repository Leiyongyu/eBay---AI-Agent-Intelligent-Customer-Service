"""现有 MySQL 业务库只读适配器（占位）。

待第 7 章 Q2/Q12 确定可读表/视图与只读账号后实现：每个查询带 store_id 过滤、超时与限流，
只读访问，不写入任何本系统数据。实现前所有调用返回 SourceNotAvailable，工具层据此返回 ERROR。
"""
from __future__ import annotations

from .base import BusinessSource, SourceNotAvailable


class MySQLSource(BusinessSource):
    name = "business_mysql"

    def _todo(self, *_args, **_kwargs):
        raise SourceNotAvailable("MySQL 业务库适配器尚未实现（待 Q2/Q12 确定表结构与只读账号）")

    updated_at = get_listing = find_products = find_orders = find_packages = get_inventory = find_cases = _todo
