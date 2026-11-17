"""eBay Browse API 只读客户端：按刊登号查刊登信息，目前用于确定会话所属站点。

GET https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id=...
使用应用令牌（client_credentials，scope=https://api.ebay.com/oauth/api_scope），只需 App ID/Cert ID，
不需要店铺授权。2026-10-24 实测：无论请求头 X-EBAY-C-MARKETPLACE-ID 填哪个站点，
返回的 listingMarketplaceId 都是刊登真正所在的站点；美国汽配频道的刊登返回 EBAY_MOTORS_US。
"""
from __future__ import annotations

import base64
import threading
import time

import httpx

from ..config import get_settings
from .auth import EbayAuthError, _json
from .message_api import EbayApiError, _api_error

APP_SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay 的细分站点代码 → 本系统站点（政策、知识按站点过滤时使用）
MARKETPLACE_ALIASES = {
    "EBAY_MOTORS_US": "EBAY_US",  # eBay Motors 是 ebay.com 上的汽配频道，政策按美国站
}


def normalize_marketplace(listing_marketplace_id: str | None) -> str | None:
    if not listing_marketplace_id:
        return None
    m = listing_marketplace_id.strip().upper()
    return MARKETPLACE_ALIASES.get(m, m)


class AppTokenManager:
    """应用令牌（不代表任何店铺），用于 Browse 等公开数据接口。"""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._expires_at - 300 > time.monotonic():
                return self._token
            s = get_settings()
            basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
            try:
                resp = self._http().post(
                    f"{s.ebay_api_base}/identity/v1/oauth2/token",
                    headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                    data={"grant_type": "client_credentials", "scope": APP_SCOPE},
                )
            except httpx.HTTPError as e:
                raise EbayAuthError(f"连接 eBay 授权服务失败：{type(e).__name__}", code="network") from None
            body = _json(resp)
            if resp.status_code != 200 or "access_token" not in body:
                code = body.get("error") or f"http_{resp.status_code}"
                raise EbayAuthError(f"获取应用令牌失败：{code} {body.get('error_description') or ''}".strip(),
                                    code=code, status=resp.status_code)
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + int(body.get("expires_in") or 7200)
            return self._token


class BrowseClient:
    def __init__(self, tokens: AppTokenManager | None = None, client: httpx.Client | None = None, sleep=time.sleep):
        self.tokens = tokens or app_token_manager
        self._client = client
        self._sleep = sleep
        self.calls = 0

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def get_item_by_legacy_id(self, legacy_item_id: str) -> dict | None:
        """返回刊登信息；刊登不存在或已下架返回 None。"""
        url = f"{get_settings().ebay_api_base}/buy/browse/v1/item/get_item_by_legacy_id"
        refreshed = False
        for attempt in range(4):
            self.calls += 1
            try:
                resp = self._http().get(url, params={"legacy_item_id": legacy_item_id}, headers={
                    "Authorization": f"Bearer {self.tokens.access_token()}", "Accept": "application/json"})
            except httpx.HTTPError as e:
                if attempt < 3:
                    self._sleep(2 * (attempt + 1))
                    continue
                raise EbayApiError(f"连接 eBay 失败：{type(e).__name__}") from None
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 401 and not refreshed:
                self.tokens.invalidate()
                refreshed = True
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < 3:
                self._sleep(2 ** attempt)
                continue
            raise _api_error(resp)
        raise EbayApiError("eBay 接口重试次数用尽")


def listing_marketplace_of(item: dict) -> str | None:
    """取刊登所在站点。只认 listingMarketplaceId：itemWebUrl 的域名会随请求头变化，不能用来判断站点。
    多属性刊登不带变体号时官方说明会返回全部变体，此时取第一个变体（同一刊登的变体站点相同）。"""
    if item.get("listingMarketplaceId"):
        return item["listingMarketplaceId"]
    for v in item.get("items") or []:
        if v.get("listingMarketplaceId"):
            return v["listingMarketplaceId"]
    return None


app_token_manager = AppTokenManager()
