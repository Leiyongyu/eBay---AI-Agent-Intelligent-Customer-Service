"""演示数据源：读取 backend/data/demo/business_fixtures.json（虚构数据，仅本地开发与回放）。"""
from __future__ import annotations

import json
from pathlib import Path

from .base import BusinessSource

FIXTURE_FILE = Path(__file__).resolve().parents[2] / "data" / "demo" / "business_fixtures.json"


class FixtureSource(BusinessSource):
    name = "demo_fixture"

    def __init__(self, path: Path = FIXTURE_FILE):
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def updated_at(self) -> str | None:
        return self.data.get("updated_at")

    def get_listing(self, store_id, item_id):
        for row in self.data["listings"]:
            if row["item_id"] == item_id and row["store_id"] == store_id:
                return row
        return None

    def find_products(self, store_id, sku):
        return [p for p in self.data["products"] if p["sku"] == sku and store_id in p["store_scope"]]

    def find_orders(self, store_id, buyer_key):
        return [o for o in self.data["orders"] if o["store_id"] == store_id and o["buyer_key"] == buyer_key]

    def find_packages(self, store_id, order_id):
        owned = {o["order_id"] for o in self.data["orders"] if o["store_id"] == store_id}
        return [p for p in self.data["packages"] if p["order_id"] == order_id and order_id in owned]

    def get_inventory(self, store_id, sku):
        for row in self.data["inventory"]:
            if row["store_id"] == store_id and row["sku"] == sku:
                return row
        return None

    def find_cases(self, store_id, order_ids):
        return [c for c in self.data["cases"] if c["store_id"] == store_id and c["order_id"] in order_ids]

    # 只给种子数据用，不是工具接口
    def stores(self) -> list[dict]:
        return self.data["stores"]
