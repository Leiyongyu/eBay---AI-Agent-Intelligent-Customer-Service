"""飞书开放平台客户端（企业自建应用的机器人）：发消息卡片、上传图片/文件、在话题里回复、读群和话题消息、下载消息里的图片。

凭证 FEISHU_APP_ID / FEISHU_APP_SECRET 只放 .env；tenant_access_token 缓存到过期前 5 分钟。不记录密钥和 token。
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from ..config import Settings, get_settings


class FeishuError(RuntimeError):
    pass


_token: dict = {"value": None, "expire": 0.0}
_lock = threading.Lock()


class FeishuClient:
    def __init__(self, settings: Settings | None = None, http: httpx.Client | None = None):
        self.s = settings or get_settings()
        self.base = self.s.feishu_domain.rstrip("/")
        self.http = http or httpx.Client(timeout=60)

    # ------------------------------------------------------------ 基础
    def token(self) -> str:
        with _lock:
            if _token["value"] and time.time() < _token["expire"]:
                return _token["value"]
            if not (self.s.feishu_app_id and self.s.feishu_app_secret):
                raise FeishuError("没有配置飞书应用（FEISHU_APP_ID / FEISHU_APP_SECRET）")
            try:
                r = self.http.post(self.base + "/open-apis/auth/v3/tenant_access_token/internal",
                                   json={"app_id": self.s.feishu_app_id, "app_secret": self.s.feishu_app_secret})
                data = r.json()
            except (httpx.HTTPError, ValueError):
                raise FeishuError("连接飞书失败") from None
            if data.get("code") != 0:
                raise FeishuError(f"飞书鉴权失败：{data.get('msg')}")
            _token["value"], _token["expire"] = data["tenant_access_token"], time.time() + int(data.get("expire", 7200)) - 300
            return _token["value"]

    def _call(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
              files: dict | None = None, data: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token()}"}
        try:
            r = self.http.request(method, self.base + path, params=params, json=body, files=files, data=data, headers=headers)
            out = r.json()
        except (httpx.HTTPError, ValueError):
            raise FeishuError(f"飞书接口请求失败：{path}") from None
        if out.get("code") != 0:
            raise FeishuError(f"飞书接口返回错误 {out.get('code')}：{str(out.get('msg'))[:200]}")
        return out.get("data") or {}

    # ------------------------------------------------------------ 发送
    def send_card(self, chat_id: str, card: dict) -> str:
        d = self._call("POST", "/open-apis/im/v1/messages", params={"receive_id_type": "chat_id"},
                       body={"receive_id": chat_id, "msg_type": "interactive",
                             "content": json.dumps(card, ensure_ascii=False)})
        return d["message_id"]

    def update_card(self, message_id: str, card: dict) -> None:
        self._call("PATCH", f"/open-apis/im/v1/messages/{message_id}", body={"content": json.dumps(card, ensure_ascii=False)})

    def upload_image(self, name: str, content: bytes, content_type: str = "image/jpeg") -> str:
        d = self._call("POST", "/open-apis/im/v1/images", data={"image_type": "message"},
                       files={"image": (name, content, content_type)})
        return d["image_key"]

    def upload_file(self, name: str, content: bytes, file_type: str = "stream") -> str:
        d = self._call("POST", "/open-apis/im/v1/files", data={"file_type": file_type, "file_name": name},
                       files={"file": (name, content, "application/octet-stream")})
        return d["file_key"]

    def reply(self, message_id: str, msg_type: str, content: dict, in_thread: bool = True) -> dict:
        """回复一条消息（in_thread=True：回在话题里）。返回 {message_id, thread_id}。"""
        d = self._call("POST", f"/open-apis/im/v1/messages/{message_id}/reply",
                       body={"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False),
                             "reply_in_thread": in_thread})
        return {"message_id": d.get("message_id"), "thread_id": d.get("thread_id")}

    def reply_text(self, message_id: str, text: str) -> dict:
        return self.reply(message_id, "text", {"text": text})

    # ------------------------------------------------------------ 读取
    def messages(self, container_type: str, container_id: str, start_time: int | None = None,
                 page_size: int = 50) -> list[dict]:
        """群（chat）或话题（thread）里的消息，旧到新；start_time 为秒级时间戳（只对群生效）。"""
        out, page = [], None
        params = {"container_id_type": container_type, "container_id": container_id, "page_size": page_size,
                  "sort_type": "ByCreateTimeAsc"}
        if start_time and container_type == "chat":
            params["start_time"] = str(int(start_time))
        for _ in range(20):
            d = self._call("GET", "/open-apis/im/v1/messages", params={**params, **({"page_token": page} if page else {})})
            out += d.get("items") or []
            page = d.get("page_token")
            if not d.get("has_more"):
                break
        return out

    def members(self, chat_id: str) -> dict[str, str]:
        d = self._call("GET", f"/open-apis/im/v1/chats/{chat_id}/members", params={"page_size": 100})
        return {m.get("member_id"): m.get("name") for m in d.get("items") or []}

    def bot_open_id(self) -> str | None:
        try:
            r = self.http.get(self.base + "/open-apis/bot/v3/info", headers={"Authorization": f"Bearer {self.token()}"})
            return (r.json().get("bot") or {}).get("open_id")
        except (httpx.HTTPError, ValueError):
            return None

    def download_image(self, message_id: str, image_key: str) -> bytes:
        try:
            r = self.http.get(self.base + f"/open-apis/im/v1/messages/{message_id}/resources/{image_key}",
                              params={"type": "image"}, headers={"Authorization": f"Bearer {self.token()}"})
        except httpx.HTTPError:
            raise FeishuError("下载飞书图片失败") from None
        if r.status_code != 200 or r.headers.get("content-type", "").startswith("application/json"):
            raise FeishuError(f"下载飞书图片失败：HTTP {r.status_code}")
        return r.content


def message_text(m: dict) -> tuple[str, list[str]]:
    """飞书消息 → (文字, 图片 key 列表)。支持 text、post（富文本）、image；@ 占位符去掉。"""
    try:
        content = json.loads((m.get("body") or {}).get("content") or "{}")
    except ValueError:
        return "", []
    t = m.get("msg_type")
    if t == "text":
        text, images = content.get("text") or "", []
    elif t == "image":
        text, images = "", [content.get("image_key")] if content.get("image_key") else []
    elif t == "post":
        rows = content.get("content") or []
        text = "\n".join("".join(x.get("text", "") for x in row if x.get("tag") in ("text", "a")) for row in rows)
        if content.get("title"):
            text = content["title"] + "\n" + text
        images = [x.get("image_key") for row in rows for x in row if x.get("tag") == "img" and x.get("image_key")]
    else:
        text, images = "", []
    for mention in m.get("mentions") or []:
        text = text.replace(mention.get("key") or "\0", "")
    return text.strip(), images
