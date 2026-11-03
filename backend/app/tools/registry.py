"""工具白名单、参数 Schema 与版本（第 12.2 节）。发送、退款、改订单永远不在这里。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from . import readonly


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    args_model: type[BaseModel]
    fn: Callable
    needs_db: bool = False  # 为 True 时执行器把当前数据库会话以 db= 传入（物流缓存要写库）


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("get_listing_context", "1", "会话关联刊登及变体（item_id 由服务端注入）",
                 readonly.NoArgs, readonly.get_listing_context),
        ToolSpec("get_item_details", "1", "当前会话商品详情：参数、兼容车型（可按车型筛选）、运费、发货时效、退货政策、描述"
                 "（模型按需请求，item_id 由服务端注入）", readonly.ItemDetailsArgs, readonly.get_item_details),
        ToolSpec("get_authorized_order_context", "1", "当前店铺、已验证买家范围内的订单",
                 readonly.OrderRefArgs, readonly.get_authorized_order_context),
        ToolSpec("get_product_by_sku", "1", "按店铺/SKU 精确查询商品资料",
                 readonly.SkuArgs, readonly.get_product_by_sku),
        ToolSpec("get_package_status", "2", "已授权订单的包裹与轨迹（eBay 发货记录 + 谷仓出库单 + 17TRACK 轨迹）",
                 readonly.OrderIdArgs, readonly.get_package_status, needs_db=True),
        ToolSpec("get_case_status", "1", "已授权订单的 return/cancellation/INR/SNAD",
                 readonly.OrderIdArgs, readonly.get_case_status),
        ToolSpec("get_inventory", "1", "按店铺/SKU 查询可售库存",
                 readonly.SkuArgs, readonly.get_inventory),
    )
}


# ---------------------------------------------------------------- 交给模型的工具（原生工具调用）
# 模型自己决定查什么；参数照样经 ToolExecutor 校验、计预算、留痕；店铺、买家、商品号仍由服务端注入，模型改不了
MODEL_TOOL_DESCRIPTIONS = {
    "get_item_details": "查询当前会话关联商品的详情：标题、SKU、参数（品牌、OE/零件号、位置、颜色等）、兼容车型、运费、"
                        "发货时效与范围、退货政策、有没有货、商品描述。买家问到这个商品本身时调用；问能否适配时把买家的车型年份填进 vehicle。",
    "get_authorized_order_context": "查询当前买家在本店最近 90 天的订单：发货状态、最晚发货时间、预计送达、付款、取消、退款状态。"
                                    "买家提供了订单号就填 order_ref。<tool_observations> 里已有订单结果时不用再查。",
    "get_package_status": "查询某个订单的物流：承运商、物流号、发货时间、当前状态、沿途最新动态和预计送达（17TRACK 轨迹）。"
                          "买家问包裹到哪了、什么时候到、显示已签收但没收到时调用。order_id 必须来自订单查询结果。",
    "get_case_status": "查询某个订单的退货、取消、未收到货、纠纷等售后进度。order_id 必须来自订单查询结果。",
    "get_product_by_sku": "按 SKU 查询本店商品资料（买家提到 SKU 时）。",
    "get_inventory": "按 SKU 查询是否有货（回复只说有没有货，不透露数量）。",
}


def model_tool_specs(has_item: bool) -> list[dict]:
    """OpenAI 兼容格式的工具定义；会话没有关联商品时不给 get_item_details。"""
    specs = []
    for name, desc in MODEL_TOOL_DESCRIPTIONS.items():
        if name == "get_item_details" and not has_item:
            continue
        params = TOOLS[name].args_model.model_json_schema()
        params.pop("title", None)
        params.setdefault("properties", {})
        specs.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return specs
