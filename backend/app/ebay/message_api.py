"""eBay Commerce Message API 只读客户端。

只实现两个 GET：
  GET /commerce/message/v1/conversation                       getConversations（每页最多 50）
  GET /commerce/message/v1/conversation/{conversation_id}     getConversation
sendMessage / updateConversation 等写接口有意不实现。
字段以 https://developer.ebay.com/develop/api/spec/message_api.json（2026-10-24 下载）为准。
"""
from __future__ import annotations

import time
from typing import Iterator

import httpx

from ..config import get_settings
from .auth import EbayTokenManager, token_manager

PAGE_LIMIT = 50  # 接口上限


class EbayApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, error_ids: list | None = None):
        super().__init__(message)
        self.status = status
        self.error_ids = error_ids or []


class MessageApiClient:
    def __init__(self, tokens: EbayTokenManager = token_manager, client: httpx.Client | None = None,
                 sleep=time.sleep):
        self.tokens = tokens
        self._client = client
        self._sleep = sleep
        self.calls = 0  # 本实例发出的请求数，便于观察调用量

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30)
        return self._client

    def _get(self, path: str, params: dict) -> dict:
        url = f"{get_settings().ebay_api_base}/commerce/message/v1{path}"
        params = {k: v for k, v in params.items() if v is not None}
        refreshed = False
        for attempt in range(4):
            self.calls += 1
            try:
                resp = self._http().get(url, params=params, headers={
                    "Authorization": f"Bearer {self.tokens.access_token()}",
                    "Accept": "application/json",
                })
            except httpx.HTTPError as e:
                if attempt < 2:
                    self._sleep(2 * (attempt + 1))
                    continue
                raise EbayApiError(f"连接 eBay 失败：{type(e).__name__}") from None
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and not refreshed:
                self.tokens.invalidate()  # 访问令牌过期或被撤销：刷新一次再试
                refreshed = True
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < 3:
                    retry_after = resp.headers.get("Retry-After")
                    self._sleep(min(30, int(retry_after)) if retry_after and retry_after.isdigit() else 2 ** attempt)
                    continue
            raise _api_error(resp)
        raise EbayApiError("eBay 接口重试次数用尽")

    def iter_conversations(self, conversation_type: str, start_time: str | None = None,
                           conversation_status: str | None = None, max_pages: int = 20) -> Iterator[dict]:
        """按页遍历会话列表。start_time 只对 FROM_MEMBERS 生效（官方说明）。"""
        offset = 0
        for _ in range(max_pages):
            data = self._get("/conversation", {
                "conversation_type": conversation_type,
                "start_time": start_time if conversation_type == "FROM_MEMBERS" else None,
                "conversation_status": conversation_status,
                "limit": PAGE_LIMIT,
                "offset": offset,
            })
            items = data.get("conversations") or []
            yield from items
            offset += len(items)
            if not items or not data.get("next") or offset >= int(data.get("total") or 0):
                return

    def get_conversation_messages(self, conversation_id: str, conversation_type: str,
                                  max_pages: int = 40) -> tuple[dict, list[dict]]:
        offset, messages, meta = 0, [], {}
        for _ in range(max_pages):
            data = self._get(f"/conversation/{conversation_id}", {
                "conversation_type": conversation_type, "limit": PAGE_LIMIT, "offset": offset})
            meta = {k: data.get(k) for k in ("conversationStatus", "conversationTitle", "conversationType", "total")}
            page = data.get("messages") or []
            messages.extend(page)
            offset += len(page)
            if not page or not data.get("next") or offset >= int(data.get("total") or 0):
                break
        return meta, messages


def _api_error(resp: httpx.Response) -> EbayApiError:
    try:
        errors = resp.json().get("errors") or []
    except ValueError:
        errors = []
    ids = [e.get("errorId") for e in errors]
    msg = "；".join(f"{e.get('errorId')} {e.get('message') or e.get('longMessage') or ''}".strip() for e in errors)
    hint = ""
    if resp.status_code == 403:
        hint = "（无权限：通常是令牌的授权范围不含 commerce.message，或该账号未开通消息接口）"
    elif resp.status_code == 401:
        hint = "（令牌无效或已过期）"
    return EbayApiError(f"eBay 接口返回 HTTP {resp.status_code}：{msg or resp.text[:200]}{hint}", resp.status_code, ids)
