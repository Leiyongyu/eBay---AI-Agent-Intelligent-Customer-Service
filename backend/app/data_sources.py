"""数据来源目录：每个功能模块的数据从哪个 eBay 接口来、带什么参数、什么时候调用、写进哪张本地表、在哪些页面用，
以及相关配置项的当前值。数据同步页按模块展示，参数里的天数、条数等取当前配置的实际值。"""
from __future__ import annotations

from .config import get_settings

# 配置项说明（不在系统配置页里的也列出来，标“只能改 .env”）
CONFIG_LABELS = {
    "ebay_sync_enabled": "定时拉取 eBay 消息", "ebay_sync_interval_seconds": "消息定时拉取间隔（秒）",
    "ebay_sync_lookback_days": "消息首次拉取天数", "ebay_live_stores": "对接 eBay 的店铺", "ebay_sync_stores": "消息拉取范围（* = 全部店铺）",
    "ebay_push_enabled": "实时推送", "ebay_notify_endpoint": "推送接收地址", "ebay_verification_token": "推送校验令牌",
    "ebay_channel": "发送渠道（ebay 真实发 / mock 只记本地）", "send_delay_seconds": "发送延迟（秒，期间可撤回）",
    "public_media_base_url": "附件公网地址前缀", "ebay_cases_sync_minutes": "售后与订单拉取间隔（分钟）",
    "orders_sync_days": "订单首次拉取天数", "ship_notice_enabled": "发货通知汇总", "ship_notice_interval_minutes": "发货通知汇总间隔（分钟）",
    "ship_notice_live_only": "发货通知只给对接店铺", "ship_notice_lookback_days": "发货通知往回看天数",
    "ship_notice_wait_for_warehouse": "等谷仓真正出库再生成", "offer_live_stores": "可以发议价报价的店铺",
    "llm_enabled": "大模型（AI 起草）", "auto_send_enabled": "AI 自动回复总开关",
    "ebay_api_base": "eBay 接口地址",
}
SECRET = {"ebay_verification_token"}


def _cfg(keys: list[str], editable: set[str]) -> list[dict]:
    s = get_settings()
    out = []
    for k in keys:
        if not hasattr(s, k):
            continue
        v = getattr(s, k)
        out.append({"key": k, "env": k.upper(), "label": CONFIG_LABELS.get(k, k),
                    "value": ("已设置" if v else "未设置") if k in SECRET else v, "editable": k in editable})
    return out


def modules() -> list[dict]:
    s = get_settings()
    from .runtime_settings import KEYS as EDITABLE

    look = s.ebay_sync_lookback_days
    msg_api = "/commerce/message/v1"
    return [
        {
            "key": "messages", "label": "买家消息", "task": "messages",
            "purpose": "把对接店铺的买家消息、eBay 系统消息拉进来：工作台列表、回复页聊天记录、AI 起草都用它",
            "pages": ["工作台（未处理 / 已处理 …）", "回复页聊天记录", "平台消息"],
            "trigger": f"定时每 {max(60, s.ebay_sync_interval_seconds)} 秒；实时推送到了立刻拉这个店铺",
            "stores": ("全部 eBay 店铺（EBAY_SYNC_STORES=*）" if (s.ebay_sync_stores or "").strip() == "*" else
                       f"这些店铺：{s.ebay_sync_stores}" if (s.ebay_sync_stores or "").strip() else "对接 eBay 的店铺（EBAY_LIVE_STORES）")
                      + "；真实发送只限对接店铺",
            "tables": ["conversations 会话", "messages 消息", "message_attachments 附件", "ebay_sync_state 拉取游标"],
            "apis": [
                {"name": "getConversations", "key": f"GET {msg_api}/conversation", "method": "GET", "path": f"{msg_api}/conversation",
                 "scope": "commerce.message", "params": f"conversation_type=FROM_MEMBERS / FROM_EBAY；start_time=上次成功时间往前 1 小时"
                                                         f"（第一次往回 {look} 天）；limit=50，翻页", "when": "每轮定时拉取、收到推送"},
                {"name": "getConversation", "key": f"GET {msg_api}/conversation/{{id}}", "method": "GET", "path": f"{msg_api}/conversation/{{conversation_id}}",
                 "scope": "commerce.message", "params": "conversation_type；limit=50，翻页取全部消息", "when": "有新消息的会话"},
                {"name": "getItemByLegacyId（Browse）", "key": "GET /buy/browse/v1/item/get_item_by_legacy_id", "method": "GET",
                 "path": "/buy/browse/v1/item/get_item_by_legacy_id", "scope": "应用令牌 api_scope",
                 "params": "legacy_item_id=会话关联的商品号", "when": "新会话补站点（商品属于哪个 eBay 站点）"},
            ],
            "config": _cfg(["ebay_sync_enabled", "ebay_sync_interval_seconds", "ebay_sync_lookback_days", "ebay_live_stores",
                            "ebay_sync_stores"], EDITABLE),
        },
        {
            "key": "push", "label": "实时推送", "task": "push",
            "purpose": "买家来信时 eBay 主动推送到本机，系统马上拉这个店铺（不用等定时拉取）",
            "pages": ["工作台（来信即到）"], "trigger": "eBay 推送（NEW_MESSAGE）", "stores": "37 个店铺都订阅；只拉对接店铺，其余只记录",
            "tables": ["ebay_notifications 推送记录", "ebay_push_subscriptions 订阅"],
            "apis": [
                {"name": "接收推送（eBay → 本机）", "key": "", "method": "POST", "path": "/webhooks/ebay/notification",
                 "scope": "签名校验（X-EBAY-SIGNATURE）", "params": "topic=NEW_MESSAGE；经 Cloudflare 隧道进来", "when": "eBay 每次推送"},
                {"name": "createDestination / createSubscription", "key": "", "method": "POST", "path": "/commerce/notification/v1/destination、/subscription",
                 "scope": "应用令牌 + 店铺 commerce.message", "params": "推送地址、校验令牌；每个店铺订阅 NEW_MESSAGE", "when": "系统状态页“启用推送订阅”时（手动）"},
            ],
            "config": _cfg(["ebay_push_enabled", "ebay_notify_endpoint", "ebay_verification_token"], EDITABLE),
        },
        {
            "key": "send", "label": "发送消息", "task": None,
            "purpose": "客服点发送、批准草稿、AI 自动回复、回执、查件结果、发货通知、发票提醒，都经发件箱延迟发出（发出前可撤回）",
            "pages": ["回复页发送", "待发送卡片（撤回）"], "trigger": f"提交后 {s.send_delay_seconds} 秒由后台发出",
            "stores": "只有对接 eBay 的店铺真实发到 eBay，其余只记在本地", "tables": ["outbound_messages 发件箱", "messages 消息"],
            "apis": [
                {"name": "sendMessage", "key": "POST /commerce/message/v1/send_message", "method": "POST", "path": f"{msg_api}/send_message",
                 "scope": "commerce.message", "params": "conversationId（已有会话）或 otherPartyUsername + reference（新会话）；messageText ≤ 2000 字；"
                                                        "messageMedia ≤ 5 个（附件公网 HTTPS 地址）", "when": "发件箱到点"},
            ],
            "config": _cfg(["ebay_channel", "ebay_live_stores", "send_delay_seconds", "public_media_base_url"], EDITABLE),
        },
        {
            "key": "cases", "label": "售后管理", "task": "cases",
            "purpose": "咨询（未收到货）、纠纷、退货、付款纠纷、评价、取消交易、退款单（只读）",
            "pages": ["售后管理 7 个菜单", "评价管理", "回复页右侧“售后”", "客户列表的退货 / 纠纷 / 差评次数"],
            "trigger": f"定时每 {s.ebay_cases_sync_minutes} 分钟（0 = 暂停）", "stores": "全部有秘钥的 eBay 店铺",
            "tables": ["ebay_cases 售后记录"],
            "apis": [
                {"name": "退货 return/search", "key": "GET /post-order/v2/return/search", "method": "GET", "path": "/post-order/v2/return/search",
                 "scope": "店铺令牌（IAF）", "params": "limit=100，offset=页码，最多 5 页", "when": "每轮"},
                {"name": "咨询 inquiry/search", "key": "GET /post-order/v2/inquiry/search", "method": "GET", "path": "/post-order/v2/inquiry/search",
                 "scope": "店铺令牌（IAF）", "params": "limit=100，最多 5 页", "when": "每轮"},
                {"name": "纠纷 casemanagement/search", "key": "GET /post-order/v2/casemanagement/search", "method": "GET",
                 "path": "/post-order/v2/casemanagement/search", "scope": "店铺令牌（IAF）", "params": "limit=100，最多 5 页", "when": "每轮"},
                {"name": "取消 cancellation/search", "key": "GET /post-order/v2/cancellation/search", "method": "GET",
                 "path": "/post-order/v2/cancellation/search", "scope": "店铺令牌（IAF）", "params": "limit=100，最多 5 页", "when": "每轮"},
                {"name": "付款纠纷 payment_dispute_summary", "key": "GET /sell/fulfillment/v1/payment_dispute_summary", "method": "GET",
                 "path": "apiz /sell/fulfillment/v1/payment_dispute_summary", "scope": "sell.payment.dispute", "params": "limit=200", "when": "每轮"},
                {"name": "退款单 Finances transaction", "key": "GET /sell/finances/v1/transaction", "method": "GET",
                 "path": "apiz /sell/finances/v1/transaction", "scope": "店铺读令牌",
                 "params": "filter=transactionType:{REFUND},transactionDate:[近 90 天..]；limit=200", "when": "每轮"},
                {"name": "评价 GetFeedback（Trading）", "key": "Trading GetFeedback", "method": "TRADING", "path": "GetFeedback",
                 "scope": "店铺令牌（IAF）", "params": "FeedbackType=FeedbackReceivedAsSeller；每店最新 200 条", "when": "每轮"},
            ],
            "config": _cfg(["ebay_cases_sync_minutes"], EDITABLE),
        },
        {
            "key": "orders", "label": "客户订单", "task": "cases",
            "purpose": "客户列表、右侧“客户资料 / 全部店铺订单”、工作台按订单号 / SKU 筛选（只存买家账号、国家、金额、状态、商品、SKU）",
            "pages": ["客户列表", "回复页右侧客户资料", "工作台筛选（订单编号、SKU）"],
            "trigger": "随售后管理一起；第一次往回拉，之后按最后修改时间增量", "stores": "全部有秘钥的 eBay 店铺",
            "tables": ["ebay_orders 订单汇总", "ebay_sync_state（ORDERS 行）游标"],
            "apis": [
                {"name": "getOrders", "key": "GET /sell/fulfillment/v1/order", "method": "GET", "path": "/sell/fulfillment/v1/order",
                 "scope": "sell.fulfillment.readonly", "params": f"filter=lastmodifieddate:[上次成功往前 2 小时..]（第一次往回 {s.orders_sync_days} 天）；"
                                                                 "limit=200，offset 翻页，最多 50 页", "when": "每轮"},
            ],
            "config": _cfg(["orders_sync_days", "ebay_cases_sync_minutes"], EDITABLE),
        },
        {
            "key": "ship_notices", "label": "发货通知", "task": "ship_notices",
            "purpose": "汇总已发货的订单，生成待审核的发货通知（订单号、承运商、跟踪号、查件链接），人工确认后发送",
            "pages": ["发货通知"], "trigger": f"定时每 {s.ship_notice_interval_minutes} 分钟",
            "stores": "对接店铺" if s.ship_notice_live_only else "全部 eBay 店铺", "tables": ["ship_notices 发货通知"],
            "apis": [
                {"name": "getOrders（已发货）", "key": "GET /sell/fulfillment/v1/order", "method": "GET", "path": "/sell/fulfillment/v1/order",
                 "scope": "sell.fulfillment.readonly",
                 "params": f"filter=lastmodifieddate:[近 {s.ship_notice_lookback_days} 天..],orderfulfillmentstatus:{{FULFILLED|IN_PROGRESS}}；limit=200",
                 "when": "每轮"},
                {"name": "getShippingFulfillments", "key": "GET /sell/fulfillment/v1/order/{id}/shipping_fulfillment", "method": "GET",
                 "path": "/sell/fulfillment/v1/order/{orderId}/shipping_fulfillment", "scope": "sell.fulfillment.readonly",
                 "params": "每个已发货订单一次：承运商、跟踪号", "when": "每轮"},
            ],
            "config": _cfg(["ship_notice_enabled", "ship_notice_interval_minutes", "ship_notice_live_only", "ship_notice_lookback_days",
                            "ship_notice_wait_for_warehouse"], EDITABLE),
        },
        {
            "key": "panel", "label": "回复页右侧（订单实时查询）", "task": None,
            "purpose": "打开会话时实时查这个买家在本店的订单、包裹、退货等（相关信息页签）；AI 起草时查订单、物流也走这里",
            "pages": ["回复页右侧相关信息", "AI 起草（大模型开启时）"], "trigger": "打开会话 / 切换订单时；结果缓存 10 分钟",
            "stores": "当前会话的店铺", "tables": ["不写库（只缓存在内存）"],
            "apis": [
                {"name": "getOrders（店铺近 90 天）", "key": "GET /sell/fulfillment/v1/order", "method": "GET", "path": "/sell/fulfillment/v1/order",
                 "scope": "sell.fulfillment.readonly", "params": "filter=creationdate:[近 90 天..]；limit=200，最多 5 页（1000 单），再按买家筛",
                 "when": "打开会话（店铺订单缓存 10 分钟）"},
                {"name": "getOrder", "key": "GET /sell/fulfillment/v1/order/{id}", "method": "GET", "path": "/sell/fulfillment/v1/order/{orderId}",
                 "scope": "sell.fulfillment.readonly", "params": "订单号", "when": "开发票、核对订单归属"},
                {"name": "getShippingFulfillments", "key": "GET /sell/fulfillment/v1/order/{id}/shipping_fulfillment", "method": "GET",
                 "path": "/sell/fulfillment/v1/order/{orderId}/shipping_fulfillment", "scope": "sell.fulfillment.readonly", "params": "订单号",
                 "when": "相关信息的包裹、物流"},
                {"name": "退货 return/search（按订单）", "key": "GET /post-order/v2/return/search", "method": "GET", "path": "/post-order/v2/return/search",
                 "scope": "店铺令牌（IAF）", "params": "order_id=订单号", "when": "AI 查售后状态"},
                {"name": "退货详情 return/{id}", "key": "GET /post-order/v2/return/{id}", "method": "GET", "path": "/post-order/v2/return/{returnId}",
                 "scope": "店铺令牌（IAF）", "params": "fieldgroups=FULL（买家寄回的跟踪号）", "when": "AI 查售后状态"},
            ],
            "config": _cfg(["ebay_api_base"], EDITABLE),
        },
        {
            "key": "listing", "label": "商品信息", "task": None,
            "purpose": "会话的商品卡片、相关信息的商品图、消息模板里的商品字段、AI 回答商品问题、按 SKU 找商品",
            "pages": ["回复页商品卡片", "相关信息商品图", "消息模板"], "trigger": "用到时查（缓存 10 分钟）",
            "stores": "当前会话的店铺（只认本店的刊登）", "tables": ["不写库"],
            "apis": [
                {"name": "GetItem（Trading）", "key": "Trading GetItem", "method": "TRADING", "path": "GetItem", "scope": "店铺令牌（IAF）",
                 "params": "ItemID；核对刊登卖家 = 本店", "when": "打开会话、模板填字段、AI 起草"},
                {"name": "GetSellerList（Trading）", "key": "Trading GetSellerList", "method": "TRADING", "path": "GetSellerList", "scope": "店铺令牌（IAF）",
                 "params": "SKUArray=SKU；EndTime 近 1 天 ~ 110 天；每页 10 条", "when": "AI 按 SKU 找商品"},
            ],
            "config": [],
        },
        {
            "key": "invoice", "label": "发票与店铺资料", "task": None,
            "purpose": "开票时读订单和店铺在 eBay 的注册资料（发票抬头）；系统状态页读店铺资料",
            "pages": ["发票", "系统状态"], "trigger": "生成发票、管理员点“读取店铺资料”时", "stores": "订单所属店铺",
            "tables": ["invoices 发票留底（读到的信息只存快照）"],
            "apis": [
                {"name": "getOrder", "key": "GET /sell/fulfillment/v1/order/{id}", "method": "GET", "path": "/sell/fulfillment/v1/order/{orderId}",
                 "scope": "sell.fulfillment.readonly", "params": "订单号", "when": "生成发票"},
                {"name": "GetUser（Trading）", "key": "Trading GetUser", "method": "TRADING", "path": "GetUser", "scope": "店铺令牌（IAF）",
                 "params": "DetailLevel=ReturnAll（注册公司名、注册人、地址）", "when": "生成发票（缓存）"},
            ],
            "config": [],
        },
        {
            "key": "best_offers", "label": "Best offer（买家报价）", "task": "best_offers",
            "purpose": "买家在刊登上出的报价：在 Best offer 页面看报价、接受 / 还价 / 拒绝（回应会真实发到 eBay，只限对接店铺）",
            "pages": ["Best offer"], "trigger": f"定时每 {s.best_offer_sync_minutes} 分钟（报价 48 小时过期；推送要 sell.offer 权限，现有授权没有）",
            "stores": "拉取：全部有秘钥的 eBay 店铺；回应：只限对接 eBay 的店铺", "tables": ["best_offers 买家报价"],
            "apis": [
                {"name": "GetBestOffers（Trading）", "key": "Trading GetBestOffers", "method": "TRADING", "path": "GetBestOffers",
                 "scope": "店铺令牌（IAF）", "params": "DetailLevel=ReturnAll；不带 ItemID = 全部处理中的报价；每页 200，最多 5 页", "when": "每轮"},
                {"name": "RespondToBestOffer（Trading）", "key": "Trading RespondToBestOffer", "method": "TRADING", "path": "RespondToBestOffer",
                 "scope": "店铺令牌（IAF）", "params": "Action=Accept / Counter / Decline；BestOfferID、ItemID；还价带 CounterOfferPrice、CounterOfferQuantity；"
                                                    "SellerResponse 留言 ≤ 250 字", "when": "在 Best offer 页面点接受 / 还价 / 拒绝（真实发出）"},
                {"name": "GetItem（Trading）", "key": "Trading GetItem", "method": "TRADING", "path": "GetItem", "scope": "店铺令牌（IAF）",
                 "params": "新报价的商品补标题、主图（有缓存）", "when": "每轮（只查新商品）"},
            ],
            "config": _cfg(["best_offer_sync_minutes", "ebay_live_stores", "ebay_channel"], EDITABLE),
        },
        {
            "key": "offers", "label": "议价（主动给感兴趣的买家发报价）", "task": None,
            "purpose": "查有感兴趣买家的商品，给他们发折扣报价（会真实发到 eBay）",
            "pages": ["议价"], "trigger": "在议价页面操作时", "stores": "可以发议价的店铺（OFFER_LIVE_STORES）",
            "tables": ["报价发送记录"],
            "apis": [
                {"name": "findEligibleItems", "key": "GET /sell/negotiation/v1/find_eligible_items", "method": "GET",
                 "path": "/sell/negotiation/v1/find_eligible_items", "scope": "sell.inventory", "params": "X-EBAY-C-MARKETPLACE-ID=站点", "when": "打开议价页"},
                {"name": "sendOfferToInterestedBuyers", "key": "POST /sell/negotiation/v1/send_offer_to_interested_buyers", "method": "POST",
                 "path": "/sell/negotiation/v1/send_offer_to_interested_buyers", "scope": "sell.inventory",
                 "params": "offeredItems（一次一个商品、折扣或价格、数量）、message", "when": "点发送报价（真实发出）"},
            ],
            "config": _cfg(["offer_live_stores"], EDITABLE),
        },
    ]
