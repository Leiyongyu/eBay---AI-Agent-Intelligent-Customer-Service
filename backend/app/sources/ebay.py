"""eBay 只读业务数据源（多店铺）：工具查询时用**会话所属店铺**的秘钥实时查 eBay。

店铺隔离：
- 每次调用都用 store_id 对应店铺自己的令牌（ebay.auth.for_store），从源头只拿得到本店数据；
- GetItem 能查到任何公开刊登，所以额外核对刊登卖家 == 本店 eBay 账号，不是本店的一律当作不存在；
- 订单、发货、售后接口本身只返回令牌所属店铺的数据；进程内缓存的键都带 store_id。
只读：REST 只发 GET；Trading 只允许 GetItem / GetSellerList / GetUser / GetStore / GetMyeBaySelling。
"""
from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..ebay.auth import READ_SCOPE, EbayAuthError, for_store
from ..models import Store
from .base import BusinessSource, SourceNotAvailable

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
TRADING_URL = "https://api.ebay.com/ws/api.dll"
READ_ONLY_TRADING = {"GetItem", "GetSellerList", "GetUser", "GetStore", "GetMyeBaySelling"}
SITE_TO_MARKETPLACE = {"US": "EBAY_US", "eBayMotors": "EBAY_US", "Germany": "EBAY_DE", "UK": "EBAY_GB",
                       "Australia": "EBAY_AU", "Austria": "EBAY_AT", "France": "EBAY_FR", "Italy": "EBAY_IT",
                       "Spain": "EBAY_ES", "Canada": "EBAY_CA", "HongKong": "EBAY_HK"}
MARKETPLACE_SITE_ID = {"EBAY_US": "0", "EBAY_GB": "3", "EBAY_DE": "77", "EBAY_AU": "15", "EBAY_FR": "71",
                       "EBAY_IT": "101", "EBAY_ES": "186", "EBAY_CA": "2", "EBAY_AT": "16"}
# 运营查看刊登用的站点域名；站点未知时用 ebay.com（刊登号在任何站点域名下都能打开）
MARKETPLACE_DOMAIN = {"EBAY_US": "ebay.com", "EBAY_GB": "ebay.co.uk", "EBAY_DE": "ebay.de", "EBAY_AU": "ebay.com.au",
                      "EBAY_FR": "ebay.fr", "EBAY_IT": "ebay.it", "EBAY_ES": "ebay.es", "EBAY_CA": "ebay.ca",
                      "EBAY_AT": "ebay.at"}
DESCRIPTION_MAX_CHARS = 1500  # 商品描述清理后最多给模型这么多字
ORDER_LOOKBACK_DAYS = 90
MAX_ORDER_PAGES = 5  # 每页 200 单，最多看最近 1000 单


class EbayReadOnlyApi:
    """一个店铺的只读 eBay 调用（白名单）。"""

    def __init__(self, store_id: str, http: httpx.Client | None = None, tokens=None):
        self.store_id = store_id
        self.http = http or httpx.Client(timeout=30)
        self.tokens = tokens or for_store(store_id, READ_SCOPE)
        self.calls = 0

    def get(self, path: str, params: dict | None = None, auth: str = "Bearer",
            marketplace: str | None = None) -> dict | None:
        """只读 GET；404 返回 None，其他错误抛 SourceNotAvailable（工具层转成 ERROR）。"""
        for attempt in range(2):
            headers = {"Authorization": f"{auth} {self._token()}", "Accept": "application/json"}
            if marketplace:
                headers["X-EBAY-C-MARKETPLACE-ID"] = marketplace
            self.calls += 1
            try:
                resp = self.http.request("GET", f"{get_settings().ebay_api_base}{path}", params=params, headers=headers)
            except httpx.HTTPError as e:
                raise SourceNotAvailable(f"连接 eBay 失败：{type(e).__name__}") from None
            if resp.status_code == 401 and attempt == 0:
                self.tokens.invalidate()
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise SourceNotAvailable(f"eBay 接口 {path} 返回 HTTP {resp.status_code}")
            return resp.json()
        raise SourceNotAvailable("eBay 令牌刷新后仍被拒绝")

    def trading(self, call: str, body: str, marketplace: str | None = None) -> ET.Element:
        if call not in READ_ONLY_TRADING:
            raise ValueError(f"{call} 不在只读白名单内")
        xml = (f"<?xml version='1.0' encoding='utf-8'?><{call}Request xmlns='urn:ebay:apis:eBLBaseComponents'>"
               f"{body}</{call}Request>")
        self.calls += 1
        try:
            resp = self.http.post(TRADING_URL, content=xml.encode("utf-8"), headers={
                "X-EBAY-API-CALL-NAME": call, "X-EBAY-API-SITEID": MARKETPLACE_SITE_ID.get(marketplace or "", "0"),
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1311", "X-EBAY-API-IAF-TOKEN": self._token(),
                "Content-Type": "text/xml"})
            return ET.fromstring(resp.content)
        except (httpx.HTTPError, ET.ParseError) as e:
            raise SourceNotAvailable(f"eBay Trading {call} 调用失败：{type(e).__name__}") from None

    def _token(self) -> str:
        try:
            return self.tokens.access_token()
        except EbayAuthError as e:
            raise SourceNotAvailable(str(e)) from None


def _t(el: ET.Element | None, path: str) -> str | None:
    return el.findtext(path, namespaces=NS) if el is not None else None


def _v(x) -> str | None:
    """Post-Order 的日期/金额常包成 {"value": ...}。"""
    if isinstance(x, dict):
        return x.get("value") or x.get("formattedValue")
    return x


def _closed(*states) -> bool:
    return any(s and "CLOSED" in str(s).upper() for s in states)


class EbaySource(BusinessSource):
    name = "ebay_api"

    def __init__(self, api_factory=None, ttl_seconds: int = 600):
        self.api_factory = api_factory or (lambda store_id: EbayReadOnlyApi(store_id))
        self.ttl = ttl_seconds
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._apis: dict[str, EbayReadOnlyApi] = {}

    # ------------------------------------------------------------ 基础
    def _api(self, store_id: str) -> EbayReadOnlyApi:
        with self._lock:
            if store_id not in self._apis:
                self._apis[store_id] = self.api_factory(store_id)
            return self._apis[store_id]

    def _cached(self, key: tuple, loader):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now + self.ttl, value)
        return value

    def _seller(self, store_id: str) -> str:
        def load():
            with session_scope() as db:
                seller = db.scalar(select(Store.ebay_user_id).where(Store.id == store_id))
            if not seller:
                raise SourceNotAvailable(f"店铺 {store_id} 的 eBay 账号未知，无法核对刊登归属")
            return seller
        return self._cached((store_id, "seller"), load)

    def _marketplace(self, store_id: str) -> str | None:
        with session_scope() as db:
            return db.scalar(select(Store.default_marketplace_id).where(Store.id == store_id))

    def updated_at(self) -> str | None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------ 刊登与商品
    def get_listing(self, store_id, item_id):
        return self._cached((store_id, "listing", item_id), lambda: self._load_listing(store_id, item_id))

    def _load_listing(self, store_id: str, item_id: str) -> dict | None:
        body = (f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
                "<IncludeItemSpecifics>true</IncludeItemSpecifics>"
                "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>")
        root = self._api(store_id).trading("GetItem", body, self._marketplace(store_id))
        item = root.find("e:Item", NS)
        if item is None:
            return None  # 刊登不存在或已删除
        if (_t(item, "e:Seller/e:UserID") or "").lower() != self._seller(store_id).lower():
            return None  # 不是本店的刊登：当作不存在，绝不返回别家数据
        return _parse_item(item)

    def find_products(self, store_id, sku):
        return self._cached((store_id, "sku", sku), lambda: self._load_products(store_id, sku))

    def _load_products(self, store_id: str, sku: str) -> list[dict]:
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        body = (f"<EndTimeFrom>{(now - timedelta(days=1)).strftime(fmt)}</EndTimeFrom>"
                f"<EndTimeTo>{(now + timedelta(days=110)).strftime(fmt)}</EndTimeTo>"
                f"<SKUArray><SKU>{_xml_escape(sku)}</SKU></SKUArray><GranularityLevel>Coarse</GranularityLevel>"
                "<Pagination><EntriesPerPage>10</EntriesPerPage><PageNumber>1</PageNumber></Pagination>")
        root = self._api(store_id).trading("GetSellerList", body, self._marketplace(store_id))
        out = []
        for it in root.findall(".//e:ItemArray/e:Item", NS)[:3]:
            listing = self.get_listing(store_id, _t(it, "e:ItemID"))
            if not listing:
                continue
            out.append({"sku": sku, "item_id": listing["item_id"], "title": listing["title"],
                        "attributes": listing["item_specifics"], "compatibility": listing["compatibility"],
                        "not_compatible": [], "listing_status": listing["listing_status"],
                        "marketplace_id": listing["marketplace_id"], "price": listing["price"],
                        "quantity_available": _variation_quantity(listing, sku)})
        return out

    def get_inventory(self, store_id, sku):
        rows = self.find_products(store_id, sku)
        if not rows:
            return None
        qty = sum(r["quantity_available"] or 0 for r in rows)
        return {"store_id": store_id, "sku": sku, "sellable_quantity": qty, "reserved_quantity": None,
                "in_transit_quantity": None, "warehouse": "eBay 刊登可售数量（刊登数量 − 已售）"}

    # ------------------------------------------------------------ 订单与发货
    def _recent_orders(self, store_id: str) -> list[dict]:
        def load():
            since = (datetime.now(timezone.utc) - timedelta(days=ORDER_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            orders, offset = [], 0
            for _ in range(MAX_ORDER_PAGES):
                data = self._api(store_id).get("/sell/fulfillment/v1/order", {
                    "filter": f"creationdate:[{since}..]", "limit": 200, "offset": offset}) or {}
                page = data.get("orders") or []
                orders += page
                offset += len(page)
                if not page or offset >= int(data.get("total") or 0):
                    break
            return orders
        return self._cached((store_id, "orders"), load)

    def find_orders(self, store_id, buyer_key):
        key = (buyer_key or "").lower()
        return [_order_row(o) for o in self._recent_orders(store_id)
                if ((o.get("buyer") or {}).get("username") or "").lower() == key]

    def _owned_order(self, store_id: str, order_id: str) -> dict | None:
        for o in self._recent_orders(store_id):
            if order_id in (o.get("orderId"), o.get("legacyOrderId")):
                return o
        return self._api(store_id).get(f"/sell/fulfillment/v1/order/{order_id}")  # 其他店铺的订单返回 404

    def invoice_order(self, store_id: str, order_id: str) -> dict | None:
        """开发票用的完整订单（含买家姓名、地址）。只给发票模块用，不进工具结果、不发给模型。"""
        return self._owned_order(store_id, order_id)

    def registration(self, store_id: str) -> dict:
        """店铺 eBay 注册资料（GetUser）：发票抬头用的注册公司名、注册人、地址、邮编（缓存 10 分钟）。"""
        return self._cached((store_id, "registration"), lambda: self._load_registration(store_id))

    def _load_registration(self, store_id: str) -> dict:
        root = self._api(store_id).trading("GetUser", "<DetailLevel>ReturnAll</DetailLevel>", self._marketplace(store_id))
        u = root.find("e:User", NS)
        a = u.find("e:RegistrationAddress", NS) if u is not None else None
        if a is None:
            raise SourceNotAvailable("eBay 没有返回店铺注册地址")
        street = list(dict.fromkeys(x.strip() for x in (_t(a, "e:Street1"), _t(a, "e:Street2"), _t(a, "e:Street")) if x and x.strip()))  # Street 常与 Street1 重复
        city = ", ".join(x for x in (_t(a, "e:CityName"), _t(a, "e:StateOrProvince")) if x)
        si = u.find("e:SellerInfo", NS)
        return {"name": _t(a, "e:CompanyName") or _t(a, "e:Name"), "contact": _t(a, "e:Name"),
                "store_url": _t(si, "e:StoreURL") if si is not None else None,  # 发票上的店铺链接和二维码
                "address": "\n".join([*street, city, _t(a, "e:CountryName") or _t(a, "e:Country") or ""]).strip(),
                "postcode": _t(a, "e:PostalCode"), "phone": _t(a, "e:Phone"),
                "ebay_user_id": _t(u, "e:UserID")}

    def find_packages(self, store_id, order_id):
        order = self._owned_order(store_id, order_id)
        if not order:
            return []
        data = self._api(store_id).get(f"/sell/fulfillment/v1/order/{order['orderId']}/shipping_fulfillment") or {}
        return [{"order_id": order["orderId"], "package_id": f.get("fulfillmentId"),
                 "carrier": f.get("shippingCarrierCode"), "tracking_number": f.get("shipmentTrackingNumber"),
                 "tracking_status": "SHIPPED", "latest_event": "已发货（eBay 只提供承运商与跟踪号，沿途节点需查承运商）",
                 "event_time": f.get("shippedDate"), "estimated_delivery": _order_row(order)["estimated_delivery"]}
                for f in data.get("fulfillments") or []]

    # ------------------------------------------------------------ 售后
    def _post_order_list(self, store_id: str, path: str, key: str) -> list[dict]:
        mk = self._marketplace(store_id)
        return self._cached((store_id, "post", path), lambda: (
            self._api(store_id).get(f"/post-order/v2/{path}", {"limit": 200}, auth="IAF", marketplace=mk) or {}
        ).get(key) or [])

    def _return_tracking(self, store_id: str, return_id, mk: str | None) -> dict:
        """买家寄回的退货包裹：承运商、跟踪号、eBay 记录的投递状态（退货详情 FULL，只读）。没寄出时返回空。"""
        if not return_id:
            return {}
        try:
            d = self._api(store_id).get(f"/post-order/v2/return/{return_id}", {"fieldgroups": "FULL"},
                                        auth="IAF", marketplace=mk) or {}
        except SourceNotAvailable:
            return {}
        trk = ((d.get("detail") or {}).get("returnShipmentInfo") or {}).get("shipmentTracking") or {}
        if not trk.get("trackingNumber"):
            return {}
        return {"return_tracking": {"carrier": trk.get("carrierName") or trk.get("carrierEnum"),
                                    "tracking_number": trk.get("trackingNumber"),
                                    "delivery_status": trk.get("deliveryStatus"),
                                    "delivered_at": _v(trk.get("actualDeliveryDate"))}}

    def find_cases(self, store_id, order_ids):
        out = []
        for order_id in order_ids:
            order = self._owned_order(store_id, order_id)
            if not order:
                continue
            ids = {order.get("orderId"), order.get("legacyOrderId")} - {None}
            legacy = order.get("legacyOrderId")
            mk = self._marketplace(store_id)
            ret = self._api(store_id).get("/post-order/v2/return/search", {"order_id": order["orderId"]},
                                          auth="IAF", marketplace=mk) or {}
            for r in ret.get("members") or []:
                out.append({"case_type": "RETURN", "case_id": str(r.get("returnId")), "order_id": order["orderId"],
                            "status": "CLOSED" if _closed(r.get("state"), r.get("status")) else "OPEN",
                            "opened_at": _v((r.get("creationInfo") or {}).get("creationDate")),
                            "due_by": _v((r.get("sellerResponseDue") or {}).get("respondByDate")),
                            "last_action": f"{r.get('state')}/{r.get('status')}",
                            **self._return_tracking(store_id, r.get("returnId"), mk)})
            for path, key, ctype, status_key, id_key in (
                    ("inquiry/search", "members", "INR", "inquiryStatusEnum", "inquiryId"),
                    ("casemanagement/search", "members", "CASE", "caseStatusEnum", "caseId")):
                for m in self._post_order_list(store_id, path, key):
                    if f"{m.get('itemId')}-{m.get('transactionId')}" == legacy:
                        out.append({"case_type": ctype, "case_id": str(m.get(id_key)), "order_id": order["orderId"],
                                    "status": "CLOSED" if _closed(m.get(status_key)) else "OPEN",
                                    "opened_at": _v(m.get("creationDate")), "due_by": _v(m.get("respondByDate")),
                                    "last_action": m.get(status_key)})
            for c in self._post_order_list(store_id, "cancellation/search", "cancellations"):
                if c.get("legacyOrderId") in ids:
                    out.append({"case_type": "CANCEL", "case_id": str(c.get("cancelId")), "order_id": order["orderId"],
                                "status": "CLOSED" if _closed(c.get("cancelState"), c.get("cancelStatus")) else "OPEN",
                                "opened_at": _v(c.get("cancelRequestDate")), "due_by": _v(c.get("sellerResponseDueDate")),
                                "last_action": f"{c.get('cancelState')}/{c.get('cancelStatus')}"})
        return out


# ---------------------------------------------------------------- 解析
def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _qty(el: ET.Element) -> int | None:
    total, sold = _t(el, "e:Quantity"), _t(el, "e:SellingStatus/e:QuantitySold")
    if total is None:
        return None
    return max(0, int(total) - int(sold or 0))


def _parse_item(item: ET.Element) -> dict:
    variations = [{"sku": _t(v, "e:SKU"),
                   "aspects": {_t(nv, "e:Name"): _t(nv, "e:Value") for nv in v.findall("e:VariationSpecifics/e:NameValueList", NS)},
                   "quantity_available": _qty(v)}
                  for v in item.findall("e:Variations/e:Variation", NS)]
    if not variations:
        variations = [{"sku": _t(item, "e:SKU"), "aspects": {}, "quantity_available": _qty(item)}]
    specifics = {}
    for nv in item.findall("e:ItemSpecifics/e:NameValueList", NS)[:40]:
        specifics[_t(nv, "e:Name")] = ", ".join(v.text or "" for v in nv.findall("e:Value", NS))
    compat = []  # 全量保存在缓存里；给模型时由工具按买家车型筛选（get_item_details）
    for c in item.findall("e:ItemCompatibilityList/e:Compatibility", NS):
        row = {_t(nv, "e:Name"): _t(nv, "e:Value") for nv in c.findall("e:NameValueList", NS) if _t(nv, "e:Name")}
        if _t(c, "e:CompatibilityNotes"):
            row["notes"] = _t(c, "e:CompatibilityNotes")
        compat.append(row)
    price = item.find("e:SellingStatus/e:CurrentPrice", NS)
    services = [_t(s, "e:ShippingService") for s in item.findall("e:ShippingDetails/e:ShippingServiceOptions", NS)]
    services += [_t(s, "e:ShippingService") for s in
                 item.findall("e:ShippingDetails/e:InternationalShippingServiceOption", NS)]
    shipping = []
    for kind, path in (("domestic", "e:ShippingDetails/e:ShippingServiceOptions"),
                       ("international", "e:ShippingDetails/e:InternationalShippingServiceOption")):
        for o in item.findall(path, NS):
            cost = o.find("e:ShippingServiceCost", NS)
            shipping.append({"type": kind, "service": _t(o, "e:ShippingService"),
                             "cost": cost.text if cost is not None else None,
                             "currency": cost.get("currencyID") if cost is not None else None,
                             "free": _t(o, "e:FreeShipping") == "true",
                             "days_min": _t(o, "e:ShippingTimeMin"), "days_max": _t(o, "e:ShippingTimeMax"),
                             "ship_to": [x.text for x in o.findall("e:ShipToLocation", NS) if x.text]})
    excluded = [x.text for x in item.findall("e:ShippingDetails/e:ExcludeShipToLocation", NS) if x.text]
    marketplace = SITE_TO_MARKETPLACE.get(_t(item, "e:Site") or "", "UNKNOWN")
    pictures = [x.text for x in item.findall("e:PictureDetails/e:PictureURL", NS) if x.text]
    return {
        "item_id": _t(item, "e:ItemID"),
        "marketplace_id": marketplace,
        "url": item_url(_t(item, "e:ItemID"), marketplace),
        "picture_url": pictures[0] if pictures else None,
        "picture_count": len(pictures),
        "sku": _t(item, "e:SKU"),
        "condition": _t(item, "e:ConditionDisplayName"),
        "category": _t(item, "e:PrimaryCategory/e:CategoryName"),
        "location": ", ".join(x for x in (_t(item, "e:Location"), _t(item, "e:Country")) if x) or None,
        "ship_to_locations": [x.text for x in item.findall("e:ShipToLocations", NS) if x.text],
        "excluded_ship_to": excluded,
        "shipping": shipping,
        "best_offer_enabled": _t(item, "e:BestOfferDetails/e:BestOfferEnabled") == "true",
        "quantity_sold": _int(_t(item, "e:SellingStatus/e:QuantitySold")),
        "description_text": description_text(_t(item, "e:Description") or ""),
        "title": _t(item, "e:Title"),
        "variations": variations,
        "item_specifics": specifics,
        "compatibility": compat,
        "compatibility_total": len(item.findall("e:ItemCompatibilityList/e:Compatibility", NS)),
        "handling_time": f"{_t(item, 'e:DispatchTimeMax')} 个工作日" if _t(item, "e:DispatchTimeMax") else None,
        "shipping_options": [s for s in services if s],
        "listing_status": _t(item, "e:SellingStatus/e:ListingStatus"),
        "price": {"value": price.text, "currency": price.get("currencyID")} if price is not None else None,
        "return_policy": {"accepted": _t(item, "e:ReturnPolicy/e:ReturnsAcceptedOption"),
                          "within": _t(item, "e:ReturnPolicy/e:ReturnsWithinOption"),
                          "shipping_paid_by": _t(item, "e:ReturnPolicy/e:ShippingCostPaidByOption"),
                          "international_accepted": _t(item, "e:ReturnPolicy/e:InternationalReturnsAcceptedOption"),
                          "international_within": _t(item, "e:ReturnPolicy/e:InternationalReturnsWithinOption"),
                          "international_shipping_paid_by":
                              _t(item, "e:ReturnPolicy/e:InternationalShippingCostPaidByOption")},
    }


def item_url(item_id: str | None, marketplace_id: str | None) -> str | None:
    if not item_id:
        return None
    return f"https://www.{MARKETPLACE_DOMAIN.get(marketplace_id or '', 'ebay.com')}/itm/{item_id}"


def description_text(raw: str, limit: int = DESCRIPTION_MAX_CHARS) -> str | None:
    """刊登描述是整页 HTML（店铺模板、样式、脚本占大头）：去掉样式脚本与标签，只留正文文字。"""
    text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("-->", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _variation_quantity(listing: dict, sku: str) -> int | None:
    for v in listing["variations"]:
        if (v["sku"] or "").upper() == sku.upper():
            return v["quantity_available"]
    return listing["variations"][0]["quantity_available"] if len(listing["variations"]) == 1 else None


def _order_row(o: dict) -> dict:
    """订单里给客服用的字段。买家姓名、地址、电话、邮箱一律不取（回复用不到，也不发给模型）。"""
    items = o.get("lineItems") or [{}]
    first = items[0]
    pay = o.get("paymentSummary") or {}
    payments = pay.get("payments") or []
    instr = [li.get("lineItemFulfillmentInstructions") or {} for li in items]
    start = (o.get("fulfillmentStartInstructions") or [{}])[0]
    step = start.get("shippingStep") or {}
    cancel = o.get("cancelStatus") or {}
    total = (o.get("pricingSummary") or {}).get("total") or {}
    est_min = start.get("minEstimatedDeliveryDate") or min((i["minEstimatedDeliveryDate"] for i in instr
                                                           if i.get("minEstimatedDeliveryDate")), default=None)
    est_max = start.get("maxEstimatedDeliveryDate") or max((i["maxEstimatedDeliveryDate"] for i in instr
                                                           if i.get("maxEstimatedDeliveryDate")), default=None)
    return {
        "order_id": o.get("orderId"), "legacy_order_id": o.get("legacyOrderId"),
        "item_id": first.get("legacyItemId"), "sku": first.get("sku"), "quantity": first.get("quantity"),
        "title": first.get("title"), "marketplace_id": first.get("listingMarketplaceId"),
        "created_at": o.get("creationDate"),
        "paid_at": payments[0].get("paymentDate") if payments else None,
        "payment_status": o.get("orderPaymentStatus"),
        # NOT_STARTED 未发货 / IN_PROGRESS 部分发货 / FULFILLED 已发货
        "order_status": o.get("orderFulfillmentStatus"),
        "total": {"value": total.get("value"), "currency": total.get("currency")} if total else None,
        # eBay 上承诺的最晚发货时间（多件取最早的）与预计送达范围
        "ship_by_date": min((i["shipByDate"] for i in instr if i.get("shipByDate")), default=None),
        "estimated_delivery": {"min": est_min, "max": est_max} if est_min or est_max else None,
        "guaranteed_delivery": any(i.get("guaranteedDelivery") for i in instr) or None,
        "shipping_service": step.get("shippingServiceCode"),
        "ship_to_country": ((step.get("shipTo") or {}).get("contactAddress") or {}).get("countryCode"),
        "has_shipment": bool(o.get("fulfillmentHrefs")),
        "cancel_state": cancel.get("cancelState"),
        "cancel_requested": bool(cancel.get("cancelRequests")),
        "refunds": len(pay.get("refunds") or []),
        "line_items": len(items),
        "items": [{"item_id": li.get("legacyItemId"), "sku": li.get("sku"), "title": li.get("title"),
                   "quantity": li.get("quantity"), "status": li.get("lineItemFulfillmentStatus")}
                  for li in items] if len(items) > 1 else None,
    }
