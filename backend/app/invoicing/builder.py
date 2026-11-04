"""把 eBay 订单整理成发票内容（纯计算，不读库、不调接口）。

金额规则：
- eBay 代收代缴了税（实测德国、英国、美国订单几乎都是）：eBay 订单金额 pricingSummary.total 是不含税价，
  税由 eBay 另外向买家收取。发票总额 = 订单金额 + eBay 代收税额（= 买家实付），税额用 eBay 的实际数，注明由 eBay 代收代缴。
- eBay 没代收税：沿用店铺现行做法（知识库 POL-INV-002），发票总额 = 订单金额（含税），
  其中增值税 = 总额 × 税率 ÷（1 + 税率），税率按收货国家取开票主体里配置的；没配的国家不列增值税。
具体税务写法以财务确认为准。
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

# 站点 → 销售渠道域名、站点所在国家
MARKETPLACE_DOMAIN = {"EBAY_DE": "ebay.de", "EBAY_GB": "ebay.co.uk", "EBAY_US": "ebay.com", "EBAY_MOTORS_US": "ebay.com",
                      "EBAY_FR": "ebay.fr", "EBAY_IT": "ebay.it", "EBAY_ES": "ebay.es", "EBAY_AT": "ebay.at",
                      "EBAY_AU": "ebay.com.au", "EBAY_CA": "ebay.ca", "EBAY_IE": "ebay.ie", "EBAY_NL": "ebay.nl",
                      "EBAY_BE": "ebay.be", "EBAY_CH": "ebay.ch", "EBAY_PL": "ebay.pl"}
MARKETPLACE_COUNTRY = {"EBAY_DE": "DE", "EBAY_GB": "GB", "EBAY_US": "US", "EBAY_MOTORS_US": "US", "EBAY_FR": "FR",
                       "EBAY_IT": "IT", "EBAY_ES": "ES", "EBAY_AT": "AT", "EBAY_AU": "AU", "EBAY_CA": "CA",
                       "EBAY_IE": "IE", "EBAY_NL": "NL", "EBAY_BE": "BE", "EBAY_CH": "CH", "EBAY_PL": "PL"}
# 站点国家（没有站点时用收货国家）→ 发票语言（其余英语）
COUNTRY_LANGUAGE = {"DE": "de", "AT": "de", "CH": "de", "LI": "de", "LU": "de", "FR": "fr", "MC": "fr",
                    "IT": "it", "SM": "it", "ES": "es"}
LANGUAGES = ("de", "en", "fr", "it", "es")
# 店铺现行税率（开具发票注意事项.txt）；开票主体没配税率时用它预填
DEFAULT_VAT_RATES = {"DE": 19, "FR": 20, "GB": 20, "IT": 22, "ES": 21}

COUNTRY_NAMES = {
    "de": {"DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "FR": "Frankreich", "IT": "Italien",
           "ES": "Spanien", "GB": "Vereinigtes Königreich", "NL": "Niederlande", "BE": "Belgien", "LU": "Luxemburg",
           "PL": "Polen", "US": "USA"},
    "en": {"DE": "Germany", "AT": "Austria", "CH": "Switzerland", "FR": "France", "IT": "Italy", "ES": "Spain",
           "GB": "United Kingdom", "NL": "Netherlands", "BE": "Belgium", "LU": "Luxembourg", "PL": "Poland",
           "US": "United States", "IE": "Ireland", "AU": "Australia", "CA": "Canada"},
    "fr": {"FR": "France", "BE": "Belgique", "DE": "Allemagne", "IT": "Italie", "ES": "Espagne", "GB": "Royaume-Uni",
           "LU": "Luxembourg", "CH": "Suisse", "MC": "Monaco"},
    "it": {"IT": "Italia", "DE": "Germania", "FR": "Francia", "ES": "Spagna", "GB": "Regno Unito", "AT": "Austria",
           "CH": "Svizzera", "SM": "San Marino"},
    "es": {"ES": "España", "DE": "Alemania", "FR": "Francia", "IT": "Italia", "GB": "Reino Unido", "PT": "Portugal"},
}


# 增值税号的国家前缀（希腊用 EL）
VAT_PREFIX = {"GR": "EL"}


def format_vat(country: str | None, number: str | None) -> str | None:
    """eBay 后台显示的税号不带国家前缀（如 DE 329615047），发票上按欧盟格式补上：DE329615047。"""
    v = re.sub(r"[\s.\-]", "", number or "").upper()
    if not v:
        return None
    prefix = VAT_PREFIX.get((country or "").upper(), (country or "").upper())
    return v if (re.match(r"^[A-Z]{2}", v) and not v[:1].isdigit() and v[:2] in (prefix, "XI", "EU")) or not prefix \
        else prefix + v


def _digits(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


class InvoiceDataError(ValueError):
    """订单数据不够开票（缺金额、缺收货地址等）。"""


def _money(m: dict | None) -> tuple[Decimal, str] | None:
    """eBay 金额：有 convertedFrom*（买家付款币种）就用它，否则用 value/currency。"""
    if not m:
        return None
    if m.get("convertedFromValue") not in (None, ""):
        return Decimal(str(m["convertedFromValue"])), m.get("convertedFromCurrency") or m.get("currency") or ""
    if m.get("value") in (None, ""):
        return None
    return Decimal(str(m["value"])), m.get("currency") or ""


def _q(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _address_lines(name: str | None, company: str | None, addr: dict | None, lang: str) -> list[str]:
    a = addr or {}
    country = a.get("countryCode") or ""
    city = " ".join(x for x in (a.get("postalCode"), a.get("city")) if x)
    lines = [company, name, a.get("addressLine1"), a.get("addressLine2"), city, a.get("stateOrProvince"),
             COUNTRY_NAMES.get(lang, {}).get(country) or COUNTRY_NAMES["en"].get(country) or country]
    out: list[str] = []
    for x in lines:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def pick_language(ship_country: str | None, marketplace_id: str | None) -> str:
    """发票语言按站点：ebay.de / ebay.at → 德语，ebay.fr → 法语，ebay.co.uk / ebay.com → 英语……
    和 eBay 订单单据一致；没有站点信息时才按收货国家。"""
    site = MARKETPLACE_COUNTRY.get(marketplace_id or "")
    if site:
        return COUNTRY_LANGUAGE.get(site, "en")
    return COUNTRY_LANGUAGE.get(ship_country or "", "en")


def last5(order_id: str) -> str:
    digits = "".join(ch for ch in order_id if ch.isdigit())
    return (digits[-5:] or "00000").rjust(5, "0")


KINDS = ("NORMAL", "VAT")  # 普通发票（同 eBay 下载的订单单据）/ 增值税发票（店铺 VAT 发票模板，带卖方税号和发票号）


def build(order: dict, *, issuer: dict, seller_account: str | None, issue_date: str, language: str | None = None,
          buyer_company: str | None = None, buyer_vat: str | None = None, kind: str = "NORMAL",
          store_name: str | None = None) -> dict:
    """返回发票内容快照（存库 + 生成 PDF 用）。warnings 给审核人看，不印在发票上。
    kind：NORMAL 普通发票 / VAT 增值税发票（订单没有增值税时不能开）。"""
    kind = kind if kind in KINDS else "NORMAL"
    items = order.get("lineItems") or []
    ps = order.get("pricingSummary") or {}
    total = _money(ps.get("total"))
    if not items or total is None:
        raise InvoiceDataError("订单缺少商品明细或总金额，不能开票")
    currency = total[1]
    start = (order.get("fulfillmentStartInstructions") or [{}])[0]
    ship = (start.get("shippingStep") or {}).get("shipTo") or {}
    ship_addr = ship.get("contactAddress") or {}
    buyer = order.get("buyer") or {}
    reg = buyer.get("buyerRegistrationAddress") or {}
    ship_country = ship_addr.get("countryCode") or (buyer.get("taxAddress") or {}).get("countryCode")
    marketplace = items[0].get("purchaseMarketplaceId") or items[0].get("listingMarketplaceId")
    lang = language if language in LANGUAGES else pick_language(ship_country, marketplace)
    warnings: list[str] = []

    lines = []
    for li in items:
        amount = _money(li.get("lineItemCost"))
        if amount is None:
            raise InvoiceDataError("订单商品缺少金额，不能开票")
        if amount[1] != currency:
            warnings.append(f"商品金额币种 {amount[1]} 与订单总额币种 {currency} 不同")
        qty = int(li.get("quantity") or 1)
        lines.append({"item_id": li.get("legacyItemId"), "sku": li.get("sku"), "title": li.get("title") or "",
                      "quantity": qty, "unit_price": str(_q(amount[0] / qty)), "amount": str(_q(amount[0])),
                      "vat_collected": bool(li.get("ebayCollectAndRemitTaxes"))})

    subtotal = _money(ps.get("priceSubtotal"))
    subtotal_v = subtotal[0] if subtotal else sum(Decimal(x["amount"]) for x in lines)
    shipping_v = (_money(ps.get("deliveryCost")) or (Decimal(0), currency))[0]
    shipping_v += (_money(ps.get("deliveryDiscount")) or (Decimal(0), currency))[0]  # 运费折扣是负数
    discount_v = -(_money(ps.get("priceDiscount")) or (Decimal(0), currency))[0]  # 商品折扣是负数，发票上写正数
    other_v = sum(((_money(ps.get(k)) or (Decimal(0), currency))[0] for k in ("adjustment", "fee")), Decimal(0))
    total_v = total[0]
    if _q(subtotal_v + shipping_v - discount_v + other_v) != _q(total_v):
        warnings.append(f"明细合计 {_q(subtotal_v + shipping_v - discount_v + other_v)} 与订单总额 {_q(total_v)} 不一致，请核对")

    rates = {k.upper(): v for k, v in (issuer.get("vat_rates") or {}).items()}
    numbers = {k.upper(): v for k, v in (issuer.get("vat_numbers") or {}).items()}
    rate = rates.get((ship_country or "").upper())
    vat_rate = Decimal(str(rate)) if rate not in (None, "") else None
    vat_country = (ship_country or "").upper() if numbers.get((ship_country or "").upper()) \
        else MARKETPLACE_COUNTRY.get(marketplace or "", "")
    vat_number = format_vat(vat_country, numbers.get(vat_country))

    taxes: dict[str, Decimal] = {}
    for li in items:
        for t in li.get("ebayCollectAndRemitTaxes") or []:
            m = _money(t.get("amount"))
            if m:
                key = t.get("taxType") or "TAX"
                taxes[key] = taxes.get(key, Decimal(0)) + m[0]
    ebay_taxes = [{"type": k, "amount": str(_q(v)), "currency": currency} for k, v in taxes.items()]
    ebay_vat = taxes.get("VAT")
    if taxes:
        tax_mode = "ebay_collected"
        gross_v = total_v + sum(taxes.values(), Decimal(0))
        vat_amount = _q(ebay_vat) if ebay_vat is not None else None
        if ebay_vat is not None and vat_rate is None and total_v:
            vat_rate = _q(ebay_vat / total_v * 100).normalize()  # 没配税率时按 eBay 税额反推
        if ebay_vat is None:
            vat_rate = None
        warnings.append(f"税由 eBay 代收代缴（{'、'.join(f'{k} {_q(v)}' for k, v in taxes.items())} {currency}），"
                        "发票总额 = 订单金额 + eBay 代收税额；税务写法以财务确认为准")
    elif vat_rate is not None:
        tax_mode = "included"
        gross_v = total_v
        vat_amount = _q(total_v * vat_rate / (Decimal(100) + vat_rate))
    else:
        tax_mode, gross_v, vat_amount = "none", total_v, None
        warnings.append(f"收货国家 {ship_country or '未知'} 没有配置税率，发票不列增值税")
    if kind == "VAT":
        if vat_amount is None:
            raise InvoiceDataError(f"收货国家 {ship_country or '未知'} 这笔订单没有增值税，不能开增值税发票，请开普通发票")
        if not vat_number:
            warnings.append(f"开票主体没有 {ship_country} 的增值税号，增值税发票上会缺卖方税号")
    refs = []
    ref_digits = {_digits((t.get("ebayReference") or {}).get("value")) for li in items
                  for t in li.get("ebayCollectAndRemitTaxes") or []} - {""}
    if kind == "VAT" and vat_number and _digits(vat_number) in ref_digits | {"200081785"}:
        warnings.insert(0, f"卖方税号 {vat_number} 是 eBay 的 OSS 税号，不是店铺自己的税号，请在开票主体里改成 eBay 后台“增值税编号”里的号码")
    for li in items:
        for t in li.get("ebayCollectAndRemitTaxes") or []:
            ref = t.get("ebayReference") or {}
            if ref.get("value") and f"{ref.get('name') or ''} - {ref['value']}" not in refs:
                refs.append(f"{ref.get('name') or ''} - {ref['value']}".strip(" -"))

    tax_id = buyer.get("taxIdentifier") or {}
    payments = (order.get("paymentSummary") or {}).get("payments") or []
    if (order.get("paymentSummary") or {}).get("refunds"):
        warnings.append("这笔订单有退款，发票仍按订单原始金额开具")
    if order.get("orderPaymentStatus") not in (None, "PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"):
        warnings.append(f"订单付款状态是 {order.get('orderPaymentStatus')}")

    start_step = (start.get("shippingStep") or {})
    return {
        "kind": kind,
        "language": lang,
        "currency": currency,
        "issue_date": issue_date,
        "order_id": order.get("orderId"),
        "order_date": (order.get("creationDate") or "")[:10],
        "paid_date": (payments[0].get("paymentDate") or "")[:10] if payments else None,
        "marketplace_id": marketplace,
        "sales_channel": MARKETPLACE_DOMAIN.get(marketplace or "", "ebay.com"),
        "seller_account": seller_account,
        "store_name": store_name or seller_account,
        "sales_record": order.get("salesRecordReference"),
        "shipping_service": start_step.get("shippingServiceCode"),
        "store_url": issuer.get("store_url"),  # 店铺链接（eBay GetUser 的 StoreURL），发票顶部印链接和二维码
        "ebay_tax_reference": refs,  # 如 ["OSS - DE 200 0817 85"]：eBay 代收增值税用的 eBay 税号
        "seller": {"name": issuer.get("name"), "contact": issuer.get("contact"), "address": issuer.get("address"),
                   "postcode": issuer.get("postcode"),
                   "phone": issuer.get("phone"), "email": issuer.get("email"), "vat_number": vat_number,
                   "footer": issuer.get("footer")},
        "bill_to": _address_lines(reg.get("fullName") or ship.get("fullName"),
                                  buyer_company or reg.get("companyName") or ship.get("companyName"),
                                  reg.get("contactAddress") or ship_addr, lang),
        "ship_to": _address_lines(ship.get("fullName"), ship.get("companyName"), ship_addr, lang),
        "ship_country": ship_country,
        "buyer_username": buyer.get("username"),
        # 和 eBay 订单单据一样印在收货地址下面：买家电话、eBay 转发邮箱（@members.ebay.com）
        "buyer_phone": ((ship.get("primaryPhone") or {}).get("phoneNumber") or "").strip() or None,
        "buyer_email": (ship.get("email") or "").strip() or None,
        "buyer_vat": buyer_vat or tax_id.get("taxpayerId"),
        "lines": lines,
        "subtotal": str(_q(subtotal_v)),
        "shipping": str(_q(shipping_v)),
        "discount": str(_q(discount_v)) if discount_v else None,
        "other": str(_q(other_v)) if other_v else None,
        "net_total": str(_q(total_v)),  # eBay 订单金额（eBay 代收税时不含税）
        "tax_mode": tax_mode,  # ebay_collected / included / none
        "total": str(_q(gross_v)),  # 发票总额（买家实付）
        "vat_rate": float(vat_rate) if vat_rate is not None else None,
        "vat_amount": str(vat_amount) if vat_amount is not None else None,
        "ebay_taxes": ebay_taxes,
        "warnings": warnings,
    }
