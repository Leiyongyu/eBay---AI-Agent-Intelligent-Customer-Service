"""eBay Commerce Message API 只读客户端。

只实现两个 GET：
  GET /commerce/message/v1/conversation                       getConversations（每页最多 50）
  GET /commerce/message/v1/conversation/{conversation_id}     getConversation
  POST /commerce/message/v1/send_message                      sendMessage（只由发件箱 sending/outbox.py 调用，延迟发出、发出前可撤回）
updateConversation 等其他写接口有意不实现。
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


class EbaySendUncertain(RuntimeError):
    """请求可能已经到了 eBay（超时、连接中断、5xx）：不能确定买家有没有收到，不能自动重发。"""


def send_message(client: "MessageApiClient", *, text: str, conversation_id: str | None = None,
                 other_party: str | None = None, item_id: str | None = None, media: list[dict] | None = None) -> dict:
    """发一条消息：已有会话用 conversationId 回复；没有会话（如发货通知）用 otherPartyUsername 新开，并关联商品。
    返回 eBay 的 SendMessageResponse（含 messageId、createdDate）。4xx 抛 EbayApiError（明确没发出），
    超时 / 连接中断 / 5xx 抛 EbaySendUncertain（结果不确定，不重发）。"""
    if not conversation_id and not other_party:
        raise EbayApiError("没有 eBay 会话 ID，也没有买家用户名，无法发送")
    body: dict = {"messageText": text[:2000]}
    if media:  # 最多 5 个，地址必须 HTTPS（mediaName、mediaType IMAGE/PDF/DOC/TXT、mediaUrl）
        body["messageMedia"] = [{"mediaName": m["mediaName"], "mediaType": m["mediaType"], "mediaUrl": m["mediaUrl"]}
                                for m in media[:5]]
    if conversation_id:
        body["conversationId"] = conversation_id
    else:
        body["otherPartyUsername"] = other_party
        if item_id:
            body["reference"] = {"referenceId": item_id, "referenceType": "LISTING"}
    url = f"{get_settings().ebay_api_base}/commerce/message/v1/send_message"
    for refreshed in (False, True):
        client.calls += 1
        try:
            resp = client._http().post(url, json=body, headers={
                "Authorization": f"Bearer {client.tokens.access_token()}",
                "Content-Type": "application/json", "Accept": "application/json"})
        except httpx.ConnectError as e:  # 连不上 eBay：请求没有发出去
            raise EbayApiError(f"连接 eBay 失败：{type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise EbaySendUncertain(f"发送请求中断（{type(e).__name__}），不确定是否已发出") from None
        if resp.status_code in (200, 201):
            try:
                return resp.json()
            except ValueError:
                return {}
        if resp.status_code == 401 and not refreshed:
            client.tokens.invalidate()  # 令牌过期：eBay 已拒绝，刷新后重发一次是安全的
            continue
        if resp.status_code >= 500:
            raise EbaySendUncertain(f"eBay 返回 HTTP {resp.status_code}，不确定是否已发出")
        raise _api_error(resp)
    raise EbayApiError("令牌刷新后仍被拒绝")


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
