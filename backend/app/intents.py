"""意图清单与默认风险（开发方案第 3.1 节）。

结构化输出、标注和评测共用这一份清单；增删意图时同步升级 RULE_VERSION。
"""
from __future__ import annotations

RULE_VERSION = "rules-2026.10.23-1"

# 编码: (中文名, 默认风险)
INTENTS: dict[str, tuple[str, str]] = {
    "item_spec": ("商品参数、规格", "L0"),
    "fitment": ("车型/设备兼容性", "L1"),
    "stock": ("库存、补货", "L1"),
    "handling_time": ("下单前问发货时效", "L1"),
    "shipping_status": ("物流进度", "L0"),
    "delivery_delay": ("超期未到、轨迹停滞", "L1"),
    "customs_duty": ("关税、进口税、清关", "L1"),
    "shipping_cost": ("运费、合并运费、改运输方式", "L1"),
    "price_offer": ("议价、批量价", "L1"),
    "invoice_vat": ("发票、VAT 发票", "L1"),
    "order_how_to": ("如何下单、付款、修改购买数量", "L0"),
    "address_change": ("修改收货地址", "L2"),
    "cancel_request": ("取消订单", "L2"),
    "return_refund": ("退货、退款咨询", "L2"),
    "refund_status": ("退货/退款进度", "L2"),
    "damaged_wrong": ("破损、错发、缺件、与描述不符", "L2"),
    "quality_issue": ("商品不工作、报故障码、漏油、异响、装上后报警等质量问题", "L2"),
    "install_help": ("安装方法、配件规格（螺栓尺寸、扭矩等）、是否需要编程等技术问题", "L2"),
    "feedback": ("评价相关", "L3"),
    "off_platform": ("要求站外联系或交易", "L3"),
    "abuse_fraud_legal": ("辱骂、诈骗、法律、安全", "L3"),
    "thanks_closing": ("致谢、确认收到", "L0"),
    "other": ("无法归类", "L1"),
}

RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


def max_risk(*levels: str | None) -> str:
    valid = [lv for lv in levels if lv in RISK_ORDER]
    return max(valid, key=RISK_ORDER.__getitem__) if valid else "L1"


def default_risk(intent: str) -> str:
    return INTENTS.get(intent, INTENTS["other"])[1]


def intent_catalog_text() -> str:
    return "\n".join(f"- {code}：{name}（默认风险 {risk}）" for code, (name, risk) in INTENTS.items())
