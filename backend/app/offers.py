"""议价：给“感兴趣的买家”发折扣报价（eBay Negotiation API）。

- GET  /sell/negotiation/v1/find_eligible_items：哪些刊登有感兴趣的买家（只返回 listingId）；
- POST /sell/negotiation/v1/send_offer_to_interested_buyers：给这些买家发报价，offeredItems 一次只能放一个商品，
  discountPercentage 和 price 二选一；
- 两个接口都要带店铺令牌（授权范围 sell.inventory）和 X-EBAY-C-MARKETPLACE-ID，站点不对会返回空；
- 数量固定 1：eBay 的报价是“全部或没有”，quantity=1 表示买家买 1 件就能按报价成交；
  可售数量只用来判断能不能发（查不到或卖完了就不发）；
- 发报价是真实对外动作：EBAY_CHANNEL=ebay，且店铺在 EBAY_LIVE_STORES（对接 eBay 真实收发）或
  OFFER_LIVE_STORES（只开议价）里才发，每次发送都记一条 offer_records。
"""
from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .db import utcnow
from .ebay.auth import SCOPE_PREFIX, EbayAuthError, for_store
from .models import AuditEvent, OfferRecord, Store

SCOPE = "https://api.ebay.com/oauth/api_scope " + SCOPE_PREFIX + "sell.inventory"
MARKETPLACES = ["EBAY_US", "EBAY_GB", "EBAY_DE", "EBAY_FR", "EBAY_IT", "EBAY_ES", "EBAY_AU", "EBAY_CA", "EBAY_AT"]
# 报价有效期：eBay 现在只接受 4 天（文档里写的 2 天已过时）。eBay 再改时会返回 150027 并写明要求的有效期，
# 发送时按它重发一次并记在 _duration 里，后面的报价直接用
DURATION_DAYS = 4
OFFER_QUANTITY = 1  # 买家买 1 件就能享受报价
_duration: dict = {"unit": "DAY", "value": DURATION_DAYS}
MIN_DISCOUNT, MAX_DISCOUNT = 5, 80
MAX_MESSAGE = 2000
PAGE = 200
CACHE_SECONDS = 300


class OfferError(RuntimeError):
    pass


# 测试时替换成假的 httpx.Client
http_factory = lambda: httpx.Client(timeout=30)  # noqa: E731
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def live_reason(store: Store | None) -> str | None:
    """不能真实发报价的原因；None = 可以发。"""
    from .sending.channel import is_live_store

    if store is None:
        return "店铺不存在"
    if get_settings().ebay_channel != "ebay":
        return "现在是本地模式（EBAY_CHANNEL 不是 ebay），不会真实发给买家"
    if not is_live_store(store) and not offer_enabled(store):
        return f"店铺「{store.name}」没有开启议价（不在“对接 eBay 的店铺”或“可发报价的店铺”里）"
    return None


def offer_enabled(store: Store) -> bool:
    """在“可发报价的店铺”（OFFER_LIVE_STORES）里：只开议价，不影响消息收发。"""
    keys = get_settings().offer_live_store_keys
    return bool(keys) and ((store.name or "").lower() in keys or store.id.lower() in keys)


def _headers(store_id: str, marketplace_id: str) -> dict:
    try:
        token = for_store(store_id, SCOPE).access_token()
    except EbayAuthError as e:
        raise OfferError(f"取店铺令牌失败：{e}（议价需要店铺授权里有 sell.inventory 权限）")
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace_id,
            "Content-Type": "application/json", "Accept": "application/json"}


def _error(resp: httpx.Response) -> str:
    try:
        errs = resp.json().get("errors") or []
        msg = "；".join(f"{e.get('errorId')}: {e.get('longMessage') or e.get('message')}" for e in errs)
    except ValueError:
        msg = resp.text[:200]
    return f"eBay 返回 {resp.status_code}：{msg or resp.reason_phrase}"


def find_eligible(store_id: str, marketplace_id: str, refresh: bool = False) -> list[str]:
    """这个店铺在这个站点有感兴趣买家的刊登号（5 分钟缓存，最多 1000 个）。"""
    key = (store_id, marketplace_id)
    hit = _cache.get(key)
    if hit and not refresh and hit[0] > time.monotonic():
        return hit[1]
    url = f"{get_settings().ebay_api_base}/sell/negotiation/v1/find_eligible_items"
    ids: list[str] = []
    with http_factory() as http:
        for offset in range(0, 1000, PAGE):
            resp = http.get(url, params={"limit": PAGE, "offset": offset}, headers=_headers(store_id, marketplace_id))
            if resp.status_code == 204:  # 没有符合条件的商品
                break
            if resp.status_code >= 400:
                raise OfferError(_error(resp))
            data = resp.json() if resp.content else {}
            page = [str(x.get("listingId")) for x in data.get("eligibleItems") or [] if x.get("listingId")]
            ids += page
            if len(page) < PAGE or offset + PAGE >= int(data.get("total") or 0):
                break
    ids = list(dict.fromkeys(ids))
    _cache[key] = (time.monotonic() + CACHE_SECONDS, ids)
    return ids


def _offer_body(listing_id: str, *, discount: str | None, price: dict | None, quantity: int, message: str | None,
                duration: dict) -> dict:
    item: dict = {"listingId": listing_id, "quantity": quantity}
    if discount is not None:
        item["discountPercentage"] = discount
    else:
        item["price"] = price
    body = {"offeredItems": [item], "offerDuration": dict(duration), "allowCounterOffer": False}
    if message:
        body["message"] = message
    return body


def required_duration(resp: httpx.Response) -> dict | None:
    """eBay 150027（有效期不对）里写的有效期，如 {"unit": "DAY", "value": 4}；不是这个错误返回 None。"""
    try:
        errs = resp.json().get("errors") or []
    except (ValueError, AttributeError):
        return None
    for e in errs:
        if str(e.get("errorId")) != "150027":
            continue
        p = {x.get("name"): x.get("value") for x in e.get("parameters") or [] if isinstance(x, dict)}
        value, unit = p.get("durationValue"), p.get("durationUnit")
        if not value:
            m = re.search(r"duration of (\d+) ([A-Za-z]+)", e.get("longMessage") or e.get("message") or "")
            if m:
                value, unit = m.groups()
        if value and str(value).isdigit():
            return {"unit": (unit or "DAY").upper(), "value": int(value)}
    return None


def listing_quantity(listing: dict | None) -> int | None:
    """刊登的可售数量（多属性刊登把各属性加起来）。"""
    if not listing:
        return None
    qty = [v.get("quantity_available") for v in listing.get("variations") or [] if isinstance(v.get("quantity_available"), int)]
    if qty:
        return sum(qty)
    q = listing.get("quantity_available")
    return q if isinstance(q, int) else None


def available_quantity(store_id: str, listing_id: str) -> int | None:
    from .sources import get_source_for_store

    return listing_quantity(get_source_for_store(store_id).get_listing(store_id, listing_id))


def validate(discount: str | None, price: dict | None, message: str | None) -> tuple[str | None, dict | None]:
    if (discount is None) == (price is None):
        raise OfferError("折扣百分比和报价金额要二选一")
    if discount is not None:
        try:
            d = Decimal(str(discount))
        except InvalidOperation:
            raise OfferError("折扣百分比要是数字")
        if not (MIN_DISCOUNT <= d <= MAX_DISCOUNT) or d != d.to_integral_value():
            raise OfferError(f"折扣要是 {MIN_DISCOUNT}–{MAX_DISCOUNT} 之间的整数（%）")
        discount = str(int(d))
    else:
        try:
            v = Decimal(str(price.get("value")))
        except (InvalidOperation, AttributeError):
            raise OfferError("报价金额要是数字")
        if v <= 0 or not price.get("currency"):
            raise OfferError("报价金额要大于 0，并写明币种")
        price = {"value": f"{v.quantize(Decimal('0.01'))}", "currency": str(price["currency"]).upper()}
    if message and len(message) > MAX_MESSAGE:
        raise OfferError(f"留言最多 {MAX_MESSAGE} 字")
    return discount, price


def send(db: Session, store: Store, marketplace_id: str, listing_ids: list[str], *, discount: str | None,
         price: dict | None, message: str | None, actor: str) -> list[dict]:
    """逐个刊登发报价（eBay 一次只收一个商品），每个都记一条结果；某个失败不影响其他。

    数量固定 1（买 1 件就能享受报价），可售数量查不到或为 0 就不发；有效期用 eBay 现在接受的天数，
    eBay 改了规则返回 150027 时按它要求的有效期重发一次，并记住给后面的报价用。
    """
    reason = live_reason(store)
    if reason:
        raise OfferError(f"不能发报价：{reason}")
    discount, price = validate(discount, price, message)
    message = (message or "").strip() or None
    url = f"{get_settings().ebay_api_base}/sell/negotiation/v1/send_offer_to_interested_buyers"
    out = []
    with http_factory() as http:
        for lid in dict.fromkeys(listing_ids):
            rec = OfferRecord(store_id=store.id, marketplace_id=marketplace_id, listing_id=lid, discount_percentage=discount,
                              price_value=price["value"] if price else None, price_currency=price["currency"] if price else None,
                              quantity=0, duration_days=_duration["value"], allow_counter=False, message=message,
                              sent_by=actor[:64], sent_at=utcnow(), status="FAILED")
            try:
                try:
                    qty = available_quantity(store.id, lid)
                except Exception as e:  # noqa: BLE001
                    raise OfferError(f"查可售数量失败（{type(e).__name__}）")
                if not qty:
                    raise OfferError("查不到这个刊登的可售数量（或已经卖完），没有发")
                rec.quantity = OFFER_QUANTITY
                body = _offer_body(lid, discount=discount, price=price, quantity=OFFER_QUANTITY, message=message,
                                   duration=_duration)
                resp = http.post(url, json=body, headers=_headers(store.id, marketplace_id))
                need = required_duration(resp) if resp.status_code == 400 else None
                if need and need != body["offerDuration"]:
                    _duration.clear()
                    _duration.update(need)
                    body["offerDuration"] = dict(need)
                    resp = http.post(url, json=body, headers=_headers(store.id, marketplace_id))
                rec.duration_days = body["offerDuration"]["value"]
                if resp.status_code >= 400:
                    rec.error = _error(resp)
                else:
                    data = resp.json() if resp.content else {}
                    offers = data.get("offers") or []
                    rec.status, rec.offers_count = "SENT", len(offers)
                    rec.response = {"offers": [{"offer_id": o.get("offerId"), "status": o.get("offerStatus"),
                                                "buyer": (o.get("buyer") or {}).get("maskedUsername")} for o in offers[:50]]}
            except OfferError as e:
                rec.error = str(e)[:300]
            except httpx.HTTPError as e:
                rec.error = f"{type(e).__name__}: {str(e)[:300]}"
            db.add(rec)
            db.flush()
            out.append(record_dict(rec))
    db.add(AuditEvent(actor=actor[:64], action="OFFERS_SENT", store_id=store.id,
                      detail={"marketplace": marketplace_id, "listings": list(listing_ids)[:50], "discount": discount,
                              "price": price, "duration": dict(_duration),
                              "sent": sum(1 for r in out if r["status"] == "SENT")}))
    return out


def record_dict(r: OfferRecord) -> dict:
    from .api.deps import iso

    return {"id": r.id, "store_id": r.store_id, "marketplace_id": r.marketplace_id, "listing_id": r.listing_id,
            "discount_percentage": r.discount_percentage,
            "price": {"value": r.price_value, "currency": r.price_currency} if r.price_value else None,
            "quantity": r.quantity, "duration_days": r.duration_days, "message": r.message, "status": r.status,
            "offers_count": r.offers_count, "response": r.response, "error": r.error, "sent_by": r.sent_by,
            "sent_at": iso(r.sent_at)}
