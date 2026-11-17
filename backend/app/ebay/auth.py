"""eBay OAuth 用户令牌管理（第 8.1 节、第 18.3 节）。

店铺授权给本应用的 refresh token 有效期很长（约 18 个月），用它向
POST {api}/identity/v1/oauth2/token 换取约 2 小时有效的访问令牌，进程内缓存，提前 5 分钟刷新。
任何日志、异常、接口返回都不包含令牌本身。
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass

import httpx

from ..config import get_settings

SCOPE_PREFIX = "https://api.ebay.com/oauth/api_scope/"
MESSAGE_SCOPE = SCOPE_PREFIX + "commerce.message"
# 只读工具用：Trading/Post-Order 需要基础 api_scope，订单与发货用 sell.fulfillment.readonly
READ_SCOPE = "https://api.ebay.com/oauth/api_scope " + SCOPE_PREFIX + "sell.fulfillment.readonly"
REFRESH_MARGIN_SECONDS = 300


class EbayAuthError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def token_kind(token: str | None) -> str:
    """粗略识别令牌类型：Auth'n'Auth（Trading API 旧令牌，以 AgAAAA 开头）不能用于 REST 接口。"""
    if not token:
        return "missing"
    t = token.strip()
    if t.startswith("AgAAAA"):
        return "auth_n_auth"
    if t.startswith("v^1.1#"):
        return "oauth"
    return "unknown"


@dataclass
class _Cached:
    token: str
    expires_at: float  # time.monotonic()
    scope: str | None


class EbayTokenManager:
    """一个店铺、一组授权范围的访问令牌缓存。

    refresh_token 不传时读 .env 的 EBAY_REFRESH_TOKEN（单店铺兼容模式）；多店铺时由 for_store() 传入一个
    每次按需从 store_credentials 解密的函数，明文只在内存里短暂存在。
    """

    def __init__(self, client: httpx.Client | None = None, refresh_token=None, scope: str = MESSAGE_SCOPE,
                 label: str = ""):
        self._client = client
        self._cached: _Cached | None = None
        self._lock = threading.Lock()
        self._refresh_token = refresh_token  # None / str / 无参函数
        self.scope = scope
        self.label = label  # 出错时提示是哪个店铺，不含令牌

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def access_token(self) -> str:
        s = get_settings()
        with self._lock:
            if self._cached and self._cached.expires_at - REFRESH_MARGIN_SECONDS > time.monotonic():
                return self._cached.token
            refresh = self._resolve_refresh_token(s)
            if refresh:
                if token_kind(refresh) == "auth_n_auth":
                    raise EbayAuthError(
                        "EBAY_REFRESH_TOKEN 是 Auth'n'Auth 旧令牌（AgAAAA 开头），Message API 不接受；"
                        "需要在开发者后台用 OAuth 方式为店铺授权并勾选 commerce.message，取得 refresh token",
                        code="auth_n_auth_token")
                self._cached = self._refresh(s, refresh)
                return self._cached.token
            if self._refresh_token is None and s.ebay_user_access_token:
                # 临时访问令牌无法续期，过期后 eBay 会返回 401
                return s.ebay_user_access_token.strip()
            raise EbayAuthError(f"{self.label}未配置 refresh token", code="missing_token")

    def _resolve_refresh_token(self, s) -> str | None:
        if self._refresh_token is None:
            return s.ebay_refresh_token
        value = self._refresh_token() if callable(self._refresh_token) else self._refresh_token
        return value.strip() if value else None

    def _refresh(self, s, refresh_token: str) -> _Cached:
        basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
        try:
            resp = self._http().post(
                f"{s.ebay_api_base}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh_token.strip(),
                      "scope": self.scope},
            )
        except httpx.HTTPError as e:
            raise EbayAuthError(f"连接 eBay 授权服务失败：{type(e).__name__}", code="network") from None
        body = _json(resp)
        if resp.status_code != 200 or "access_token" not in body:
            code = body.get("error") or f"http_{resp.status_code}"
            hint = {
                "invalid_client": "App ID / Cert ID 不正确，或与 EBAY_ENV（生产/沙箱）不匹配",
                "invalid_grant": "refresh token 无效、已过期或已被撤销，或不是本 App ID 签发的",
                "invalid_scope": f"该 refresh token 的授权范围里没有 {self.scope.replace(SCOPE_PREFIX, '')}，需要重新为店铺授权并勾选对应权限",
            }.get(code, "")
            desc = body.get("error_description") or ""
            raise EbayAuthError(f"{self.label}刷新访问令牌失败：{code} {desc} {hint}".strip(), code=code,
                                status=resp.status_code)
        expires_in = int(body.get("expires_in") or 7200)
        return _Cached(body["access_token"], time.monotonic() + expires_in, body.get("scope"))


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


token_manager = EbayTokenManager()


# ---------------------------------------------------------------- 多店铺
_store_managers: dict[tuple[str, str], EbayTokenManager] = {}
_store_lock = threading.Lock()


def resolve_store_refresh_token(store_id: str) -> str:
    """店铺的 refresh token：优先从 store_credentials 解密；表里没有且是 .env 配的那个店铺时用 .env。"""
    from ..db import session_scope
    from ..security.store_credentials import get_credential_by_store_id

    with session_scope() as db:
        cred = get_credential_by_store_id(db, store_id)
    if cred and cred.secret:
        return cred.secret
    s = get_settings()
    if store_id == s.ebay_store_id and s.ebay_refresh_token:
        return s.ebay_refresh_token
    raise EbayAuthError(f"店铺 {store_id} 没有可用的秘钥（store_credentials 中无记录）", code="missing_token")


def for_store(store_id: str, scope: str = MESSAGE_SCOPE, client: httpx.Client | None = None) -> EbayTokenManager:
    """按店铺、授权范围取令牌管理器（进程内缓存访问令牌，不缓存 refresh token 明文）。"""
    with _store_lock:
        key = (store_id, scope)
        if key not in _store_managers or client is not None:
            _store_managers[key] = EbayTokenManager(client, refresh_token=lambda: resolve_store_refresh_token(store_id),
                                                    scope=scope, label=f"店铺 {store_id}：")
        return _store_managers[key]


def reset_store_managers() -> None:
    with _store_lock:
        _store_managers.clear()
