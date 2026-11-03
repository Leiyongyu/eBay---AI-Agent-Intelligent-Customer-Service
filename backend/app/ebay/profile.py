"""用店铺秘钥（refresh token）只读查询 eBay 卖家与店铺信息。

只调用 Trading API 的 GetUser、GetStore 两个查询接口，不做任何修改；令牌只在内存中使用，不写日志。
"""
from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from ..config import get_settings

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
READ_ONLY_CALLS = {"GetUser", "GetStore"}
TRADING_URL = "https://api.ebay.com/ws/api.dll"


@dataclass
class SellerProfile:
    user_id: str | None = None
    store_name: str | None = None
    store_url: str | None = None
    registration_site: str | None = None
    error: str | None = None


def _access_token(client: httpx.Client, refresh_token: str) -> str:
    s = get_settings()
    basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
    resp = client.post(f"{s.ebay_api_base}/identity/v1/oauth2/token",
                       headers={"Authorization": f"Basic {basic}"},
                       data={"grant_type": "refresh_token", "refresh_token": refresh_token.strip(),
                             "scope": "https://api.ebay.com/oauth/api_scope"})
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if "access_token" not in body:
        raise RuntimeError(f"换取访问令牌失败：{body.get('error') or resp.status_code}")
    return body["access_token"]


def _trading(client: httpx.Client, token: str, call: str, body: str = "") -> ET.Element:
    if call not in READ_ONLY_CALLS:
        raise ValueError(f"{call} 不在只读白名单内")
    xml = f"<?xml version='1.0' encoding='utf-8'?><{call}Request xmlns='urn:ebay:apis:eBLBaseComponents'>{body}</{call}Request>"
    resp = client.post(TRADING_URL, content=xml.encode(), headers={
        "X-EBAY-API-CALL-NAME": call, "X-EBAY-API-SITEID": "0", "X-EBAY-API-COMPATIBILITY-LEVEL": "1311",
        "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml"})
    return ET.fromstring(resp.content)


def fetch_seller_profile(refresh_token: str, client: httpx.Client | None = None) -> SellerProfile:
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        token = _access_token(client, refresh_token)
        root = _trading(client, token, "GetUser")
        if root.findtext("e:Ack", namespaces=NS) not in ("Success", "Warning"):
            return SellerProfile(error=f"GetUser 失败：{root.findtext('.//e:ShortMessage', namespaces=NS)}"[:300])
        user = root.find("e:User", NS)
        profile = SellerProfile(user_id=user.findtext("e:UserID", namespaces=NS),
                                registration_site=user.findtext("e:Site", namespaces=NS),
                                store_url=user.findtext("e:SellerInfo/e:StoreURL", namespaces=NS))
        if user.findtext("e:SellerInfo/e:StoreOwner", namespaces=NS) == "true":
            store = _trading(client, token, "GetStore")
            if store.findtext("e:Ack", namespaces=NS) in ("Success", "Warning"):
                profile.store_name = store.findtext("e:Store/e:Name", namespaces=NS)
                profile.store_url = store.findtext("e:Store/e:URL", namespaces=NS) or profile.store_url
        return profile
    except Exception as e:  # 单个店铺失败只记原因，不影响其他店铺
        return SellerProfile(error=f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if own:
            client.close()
