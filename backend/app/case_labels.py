"""售后管理（咨询、纠纷、退货、付款纠纷、评价、取消交易、退款单）里 eBay 原样值的中文名。

状态、原因、退货类型、卖家要做的动作、发起方、关闭原因、付款状态等，页面都显示中文；不认识的值原样显示，以后在这里补。
退货、取消的状态是“state/status”两段，分别翻译后合并（意思重复的只显示一个）。
"""
from __future__ import annotations

STATUS: dict[str, str] = {
    # 通用
    "OPEN": "处理中", "CLOSED": "已关闭", "PENDING": "待处理", "ON_HOLD": "暂缓处理", "ESCALATED": "已升级到 eBay",
    # 咨询（未收到货）/ 纠纷
    "WAITING_BUYER_RESPONSE": "等待买家回复", "WAITING_SELLER_RESPONSE": "等待卖家回复", "CS_CLOSED": "eBay 客服已关闭",
    "CS_WAITING_FOR_SELLER": "等待卖家（eBay 客服介入）", "CS_WAITING_FOR_BUYER": "等待买家（eBay 客服介入）",
    "CS_OPEN": "eBay 客服处理中", "WAITING_FOR_CS": "等待 eBay 客服处理", "OTHER": "其他",
    # 退货 state
    "RETURN_REQUESTED": "买家申请退货", "RETURN_REQUEST_TIMEOUT": "退货申请超时未处理", "RETURN_REQUEST_REJECTED": "已拒绝退货申请",
    "RETURN_LABEL_PENDING": "等待提供退货标签", "RETURN_LABEL_PENDING_TIMEOUT": "提供退货标签超时",
    "ITEM_READY_TO_SHIP": "等待买家寄回", "ITEM_SHIPPED": "买家已寄回", "ITEM_DELIVERED": "退货已送达",
    "ITEM_KEPT": "买家保留商品", "REPLACED": "已换货", "REPLACEMENT_SHIPPED": "换货已发出",
    "LESS_THAN_A_FULL_REFUND_ISSUED": "已部分退款", "PARTIAL_REFUND_REQUESTED": "买家申请部分退款",
    "PARTIAL_REFUND_DECLINED": "已拒绝部分退款", "PARTIAL_REFUND_INITIATED": "部分退款处理中",
    "REFUND_INITIATED": "退款处理中", "REFUND_ISSUED": "已退款", "REFUND_SENT": "已退款", "REFUND_TIMEOUT": "退款超时",
    "FULL_REFUND_ISSUED": "已全额退款",
    # 退货 status
    "READY_FOR_SHIPPING": "等待寄回", "WAITING_FOR_RETURN_LABEL": "等待退货标签", "WAITING_FOR_RMA": "等待退货授权号",
    "WAITING_FOR_RETURN_SHIPMENT": "等待买家寄回", "RETURN_SHIPPED": "买家已寄回",
    # 取消 state / status
    "INITIATED": "已发起", "CANCEL_REQUESTED": "买家申请取消", "CANCEL_PENDING": "取消处理中",
    "CANCEL_CLOSED_FOR_COMMITMENT": "已关闭（买家须继续购买）", "CANCEL_CLOSED_WITH_REFUND": "已取消并退款",
    "CANCEL_CLOSED_NO_REFUND": "已取消（无需退款）", "CANCEL_SUCCESS_NO_REFUND": "已取消（无需退款）",
    "CANCEL_REJECTED": "卖家拒绝取消", "CANCEL_SUCCESS": "已取消", "CANCEL_FAILED": "取消失败",
    "CANCEL_CLOSED_UNKNOWN_REFUND": "已取消（退款情况未知）",
    # 付款纠纷
    "ACTION_NEEDED": "需要卖家处理", "WAITING_FOR_BUYER": "等待买家", "WAITING_FOR_SELLER": "等待卖家",
    # 退款单（Finances 交易状态）
    "FUNDS_AVAILABLE_FOR_PAYOUT": "可结算", "PAYOUT": "已结算", "FUNDS_PROCESSING": "资金处理中", "FUNDS_ON_HOLD": "资金冻结中",
    "COMPLETED": "已完成", "FAILED": "失败",
    # 评价
    "Positive": "好评", "Neutral": "中评", "Negative": "差评", "Withdrawn": "已撤回",
}

REASON: dict[str, str] = {
    # 退货原因
    "ARRIVED_DAMAGED": "到货损坏", "ARRIVED_LATE": "到货太晚", "DEFECTIVE_ITEM": "商品有缺陷", "DOES_NOT_FIT": "不适配",
    "FOUND_BETTER_PRICE": "找到更便宜的", "MISSING_PARTS": "缺少配件", "NOT_AS_DESCRIBED": "与描述不符",
    "NO_LONGER_NEED_ITEM": "不需要了", "NO_REASON": "未说明原因", "ORDERED_ACCIDENTALLY": "误下单",
    "ORDERED_DIFFERENT_ITEM": "想买的是别的商品", "ORDERED_WRONG_ITEM": "买错了", "WRONG_SIZE": "尺寸不对",
    "WITHDRAW_FROM_PURCHASE_CONTRACT": "撤销购买（欧盟无理由退货）", "WRONG_ITEM": "发错货", "WRONG_COLOR": "颜色不对",
    "FAKE_OR_COUNTERFEIT": "假冒商品", "EXPIRED_ITEM": "商品过期", "DIFFERENT_FROM_LISTING": "与刊登不符",
    "BUYER_CANCEL_ORDER": "买家取消订单", "BUYER_NO_SHOW": "买家未取货", "BUYER_REFUSED_TO_PICKUP": "买家拒绝取货",
    "IN_STORE_RETURN": "到店退货", "OTHER": "其他",
    # 取消原因
    "BUYER_ASKED_CANCEL": "买家要求取消", "FOUND_CHEAPER_PRICE": "找到更便宜的", "ORDER_MISTAKE": "下错单",
    "ORDER_UNPAID": "未付款", "OUT_OF_STOCK_OR_CANNOT_FULFILL": "缺货或无法发货", "WONT_ARRIVE_IN_TIME": "不能按时送达",
    "WRONG_PAYMENT_METHOD": "付款方式错误", "WRONG_SHIPPING_ADDRESS": "收货地址错误", "WRONG_SHIPPING_METHOD": "运输方式错误",
    "ADDRESS_ISSUES": "地址问题", "UNPAID": "未付款",
    # 付款纠纷原因
    "CREDIT_NOT_PROCESSED": "没收到退款", "FRAUD": "未经授权的交易（欺诈）", "ITEM_NOT_RECEIVED": "没收到货",
    "SIGNIFICANTLY_NOT_AS_DESCRIBED": "与描述严重不符", "TRANSACTION_ISSUE": "交易问题", "DUPLICATE_TRANSACTION": "重复扣款",
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "没收到货", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "与描述不符",
    "UNAUTHORIZED": "未经授权的交易", "INCORRECT_AMOUNT": "金额不对",
}

DETAIL_KEYS = {
    "type": "退货类型", "seller_action": "卖家需要做", "requestor": "发起方", "close_reason": "关闭原因",
    "payment_status": "付款状态", "booking": "记账方向", "action_url": "eBay 处理链接", "responded": "已回复",
    "closed_date": "关闭时间",
}
DETAIL_VALUES = {
    "MONEY_BACK": "退款", "REPLACEMENT": "换货", "EXCHANGE": "换货",
    "SELLER_APPROVE_REQUEST": "同意退货申请", "SELLER_ISSUE_REFUND": "给买家退款", "SELLER_PROVIDE_LABEL": "提供退货标签",
    "SELLER_RESPOND": "回复买家", "SELLER_MARK_REFUND_SENT": "确认已退款", "SELLER_CHECK_REPLACEMENT": "确认换货",
    "BUYER": "买家", "SELLER": "卖家", "EBAY": "eBay", "SYSTEM": "系统",
    "FULL_REFUNDED": "已全额退款", "SELLER_APPROVE_UNPAID": "卖家同意（订单未付款）", "SELLER_CANCEL_UNPAID": "卖家取消未付款订单",
    "SELLER_CANCEL_UNPAID_STRIKE": "卖家取消未付款订单（记卖家缺陷）", "SELLER_DECLINE": "卖家拒绝",
    "SYSTEM_APPROVED_CANCEL_WITH_PAYMENT_CANCELLATION": "系统同意取消并撤销付款",
    "SYSTEM_NOTIFY_SHIPPING_STATUS": "系统通知已发货（取消关闭）", "BUYER_WITHDREW": "买家撤回", "SELLER_APPROVED": "卖家同意",
    "NOT_PAID": "未付款", "ONLINE_PAID": "已付款", "PAID": "已付款",
    "DEBIT": "扣款", "CREDIT": "入账", True: "是", False: "否",
}


# 退货、取消常见的“state/status”组合：直接给一句通顺的中文
COMBO = {
    "CLOSED/ESCALATED": "已关闭（曾升级到 eBay）",
    "ITEM_READY_TO_SHIP/READY_FOR_SHIPPING": "等待买家寄回",
    "RETURN_LABEL_PENDING/WAITING_FOR_RETURN_LABEL": "等待提供退货标签",
    "RETURN_LABEL_PENDING_TIMEOUT/WAITING_FOR_RETURN_LABEL": "提供退货标签超时",
    "RETURN_REQUEST_TIMEOUT/RETURN_REQUESTED": "退货申请超时未处理",
}


def status_label(value: str | None) -> str | None:
    """单个或“state/status”两段的状态 → 中文；意思重复的只显示一个，不认识的原样保留。"""
    if not value:
        return value
    if value in COMBO:
        return COMBO[value]
    parts = [p for p in str(value).split("/") if p and p != "None"]
    if len(parts) == 2 and parts[0] == "CLOSED" and parts[1] != "CLOSED":  # 取消等：已关闭/具体结果 → 只说结果
        return STATUS.get(parts[1], parts[1])
    out: list[str] = []
    for p in parts:
        zh = STATUS.get(p, p)
        if zh not in out:
            out.append(zh)
    if len(out) == 2 and out[0] == "已关闭" and out[1] in ("已关闭", "eBay 客服已关闭"):
        out = out[1:]
    return " · ".join(out) or value


def reason_label(value: str | None) -> str | None:
    if not value:
        return value
    return REASON.get(value, STATUS.get(value, value))


def detail_label(key: str, value) -> str:
    if isinstance(value, bool):
        return DETAIL_VALUES[value]
    if key in ("action_url", "closed_date"):
        return value
    return DETAIL_VALUES.get(value, REASON.get(value, STATUS.get(value, value))) if isinstance(value, str) else value


def detail_labels(detail: dict | None) -> list[dict]:
    """详情里的其他字段：[{key, key_label, value, value_label}]，跳过空值。"""
    out = []
    for k, v in (detail or {}).items():
        if v in (None, "", []):
            continue
        out.append({"key": k, "key_label": DETAIL_KEYS.get(k, k), "value": v, "value_label": detail_label(k, v)})
    return out
