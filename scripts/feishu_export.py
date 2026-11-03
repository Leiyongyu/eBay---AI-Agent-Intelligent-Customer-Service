"""飞书群聊历史导出（临时工具）：PyCharm 里直接 Run 本文件。

读取根目录 .env 中的 FEISHU_APP_ID / FEISHU_APP_SECRET，导出机器人所在的群（或 FEISHU_CHAT_IDS、
FEISHU_CHAT_NAME_KEYWORD 指定的群）的全部历史消息、话题回复、合并转发里的子消息，以及图片、文件、
语音、视频附件，写到 FEISHU_EXPORT_DIR（默认 data/feishu_export，已在 .gitignore 中）：

    <导出目录>/<群名>_<chat_id>/
        chat.json        群信息与成员（open_id → 姓名）
        messages.jsonl   原始消息，一行一条，按 message_id 去重；重复运行只追加新消息
        messages.md      便于阅读的时间线
        files/           附件，文件名 <message_id>_<原文件名或 key>
        errors.jsonl     下载失败、无权限等记录

前提：飞书开放平台的企业自建应用已开启机器人能力，开通 im:chat:readonly、im:message:readonly、
“获取群组中所有消息”等权限并发布了版本，机器人已被拉进目标群。
只用标准库；不打印 app_secret 和 token。导出内容含买家与同事的个人信息，仅限内部使用（开发方案第 18.2 节）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TOKEN_ERROR_CODES = {99991661, 99991663, 99991668}
RATE_LIMIT_CODE = 99991400
REQUEST_INTERVAL = 0.12  # 约 8 次/秒，低于开放平台常见限流
MAX_RETRIES = 5


class FeishuError(Exception):
    def __init__(self, code: int, msg: str, path: str):
        super().__init__(f"[{code}] {msg}（{path}）")
        self.code = code


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in list(values) + [k for k in os.environ if k.startswith("FEISHU_")]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def to_timestamp(day: str | None) -> str | None:
    if not day:
        return None
    return str(int(datetime.strptime(day, "%Y-%m-%d").timestamp()))


def safe_name(name: str, limit: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return (name or "unnamed")[:limit]


class FeishuClient:
    def __init__(self, domain: str, app_id: str, app_secret: str):
        self.domain = domain.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._token_expire_at = 0.0
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = REQUEST_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _refresh_token(self) -> None:
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        req = urllib.request.Request(
            f"{self.domain}/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            raise FeishuError(data.get("code", -1), data.get("msg", ""), "tenant_access_token")
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200))

    def _auth_header(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expire_at - 300:
            self._refresh_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _open(self, path: str, params: dict | None):
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.domain}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            req = urllib.request.Request(url, headers=self._auth_header())
            try:
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"code": e.code, "msg": raw[:200].decode(errors="replace")}
                code = data.get("code", e.code)
                if code in TOKEN_ERROR_CODES:
                    self._token = None
                    continue
                if e.code == 429 or code == RATE_LIMIT_CODE or e.code >= 500:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise FeishuError(code, data.get("msg", ""), path) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == MAX_RETRIES:
                    raise
                print(f"  网络异常，{2 ** attempt} 秒后重试：{e}")
                time.sleep(2 ** attempt)
        raise FeishuError(-1, "重试次数用尽", path)

    def get(self, path: str, params: dict | None = None) -> dict:
        for _ in range(MAX_RETRIES):
            with self._open(path, params) as resp:
                data = json.loads(resp.read())
            code = data.get("code", 0)
            if code == 0:
                return data.get("data") or {}
            if code in TOKEN_ERROR_CODES:
                self._token = None
                continue
            if code == RATE_LIMIT_CODE:
                time.sleep(5)
                continue
            raise FeishuError(code, data.get("msg", ""), path)
        raise FeishuError(-1, "重试次数用尽", path)

    def paginate(self, path: str, params: dict, max_items: int | None = None) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            data = self.get(path, {**params, "page_token": page_token})
            items.extend(data.get("items") or [])
            if max_items and len(items) >= max_items:
                return items[:max_items]
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")
            if not page_token:
                return items

    def download(self, message_id: str, file_key: str, kind: str, target_dir: Path, preferred_name: str) -> Path:
        path = f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
        with self._open(path, {"type": kind}) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if content_type.startswith("application/json"):
                data = json.loads(resp.read())
                raise FeishuError(data.get("code", -1), data.get("msg", ""), path)
            name = preferred_name
            if "." not in name:
                disposition = resp.headers.get("Content-Disposition", "")
                match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
                if match and "." in match.group(1):
                    # 保留 key 作文件名前缀（续导时据此判断已下载），只取服务端文件名的扩展名
                    name += "." + urllib.parse.unquote(match.group(1)).rsplit(".", 1)[-1]
                else:
                    name += mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
            target = target_dir / safe_name(f"{message_id}_{name}")
            tmp = target.with_suffix(target.suffix + ".part")
            with open(tmp, "wb") as f:
                while chunk := resp.read(1024 * 256):
                    f.write(chunk)
            tmp.replace(target)
            return target


# ---------------- 消息内容解析 ----------------

def parse_content(message: dict) -> dict:
    try:
        return json.loads((message.get("body") or {}).get("content") or "{}")
    except ValueError:
        return {}


def post_blocks(content: dict) -> tuple[str, list]:
    """富文本可能直接是 {title, content}，也可能按语言包一层 {zh_cn: {...}}。"""
    if "content" not in content and content:
        content = next((v for v in content.values() if isinstance(v, dict)), {})
    return content.get("title") or "", content.get("content") or []


def resources_of(message: dict) -> list[tuple[str, str, str]]:
    """返回 [(file_key, 下载类型 image/file, 建议文件名)]。"""
    msg_type = message.get("msg_type")
    content = parse_content(message)
    found: list[tuple[str, str, str]] = []
    if msg_type == "image" and content.get("image_key"):
        found.append((content["image_key"], "image", content["image_key"]))
    elif msg_type in ("file", "audio", "media") and content.get("file_key"):
        found.append((content["file_key"], "file", content.get("file_name") or content["file_key"]))
    elif msg_type == "post":
        _, blocks = post_blocks(content)
        for line in blocks:
            for el in line or []:
                if el.get("tag") == "img" and el.get("image_key"):
                    found.append((el["image_key"], "image", el["image_key"]))
                elif el.get("tag") == "media" and el.get("file_key"):
                    found.append((el["file_key"], "file", el.get("file_name") or el["file_key"]))
    return found


def readable_text(message: dict, names: dict[str, str]) -> str:
    if message.get("deleted"):
        return "[已撤回]"
    msg_type = message.get("msg_type")
    content = parse_content(message)
    if msg_type == "text":
        text = content.get("text", "")
        for m in message.get("mentions") or []:
            text = text.replace(m.get("key", ""), f"@{m.get('name') or names.get(m.get('id'), '')}")
        return text.replace("@_all", "@所有人")
    if msg_type == "post":
        title, blocks = post_blocks(content)
        parts = [f"【{title}】"] if title else []
        for line in blocks:
            pieces = []
            for el in line or []:
                tag = el.get("tag")
                if tag in ("text", "md"):
                    pieces.append(el.get("text", ""))
                elif tag == "a":
                    pieces.append(f"{el.get('text', '')}({el.get('href', '')})")
                elif tag == "at":
                    pieces.append(f"@{el.get('user_name') or names.get(el.get('user_id'), '')}")
                elif tag == "img":
                    pieces.append("[图片]")
                elif tag == "media":
                    pieces.append("[视频]")
                elif tag == "emotion":
                    pieces.append(f"[{el.get('emoji_type', '表情')}]")
                elif tag == "code_block":
                    pieces.append(el.get("text", ""))
            parts.append("".join(pieces))
        return "\n".join(p for p in parts if p)
    labels = {
        "image": "[图片]", "file": f"[文件] {content.get('file_name', '')}", "audio": "[语音]",
        "media": f"[视频] {content.get('file_name', '')}", "sticker": "[表情]",
        "merge_forward": "[合并转发]", "interactive": "[卡片消息]", "share_chat": "[分享群名片]",
        "share_user": "[分享个人名片]", "system": "[系统消息]", "location": "[位置]",
    }
    return labels.get(msg_type, f"[{msg_type}]")


# ---------------- 单个群的导出 ----------------

class ChatExporter:
    def __init__(self, client: FeishuClient, chat: dict, out_root: Path, cfg: dict):
        self.client = client
        self.chat = chat
        self.chat_id = chat["chat_id"]
        self.dir = out_root / safe_name(f"{chat.get('name') or '未命名群'}_{self.chat_id}")
        self.files_dir = self.dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.messages_file = self.dir / "messages.jsonl"
        self.errors_file = self.dir / "errors.jsonl"
        self.cfg = cfg
        self.names: dict[str, str] = {}
        self.messages: dict[str, dict] = {}
        self.stats = {"新消息": 0, "话题": 0, "合并转发": 0, "附件": 0, "附件已存在": 0, "错误": 0}
        if self.messages_file.exists():
            for line in self.messages_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    m = json.loads(line)
                    self.messages[m["message_id"]] = m

    def log_error(self, **record) -> None:
        self.stats["错误"] += 1
        record["time"] = datetime.now().isoformat(timespec="seconds")
        with open(self.errors_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def add(self, message: dict, source: str) -> bool:
        mid = message.get("message_id")
        if not mid or mid in self.messages:
            return False
        message["_source"] = source
        self.messages[mid] = message
        with open(self.messages_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.stats["新消息"] += 1
        return True

    def load_members(self) -> None:
        try:
            members = self.client.paginate(
                f"/open-apis/im/v1/chats/{self.chat_id}/members",
                {"member_id_type": "open_id", "page_size": 100},
            )
            self.names = {m["member_id"]: m.get("name", "") for m in members if m.get("member_id")}
        except FeishuError as e:
            print(f"  ⚠ 读取群成员失败，时间线里将显示 open_id：{e}")
            self.log_error(step="members", error=str(e))
        (self.dir / "chat.json").write_text(
            json.dumps({"chat": self.chat, "members": self.names}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def fetch_container(self, container_type: str, container_id: str, source: str) -> list[dict]:
        params = {
            "container_id_type": container_type,
            "container_id": container_id,
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if container_type == "chat":
            params["start_time"] = self.cfg["start_time"]
            params["end_time"] = self.cfg["end_time"]
        items = self.client.paginate("/open-apis/im/v1/messages", params, self.cfg["max_messages"])
        for m in items:
            self.add(m, source)
        return items

    def expand_merge_forward(self, message: dict, depth: int = 0) -> None:
        if depth > 3:
            return
        try:
            data = self.client.get(f"/open-apis/im/v1/messages/{message['message_id']}")
        except FeishuError as e:
            self.log_error(step="merge_forward", message_id=message["message_id"], error=str(e))
            return
        for child in data.get("items") or []:
            if child.get("message_id") == message["message_id"]:
                continue
            child.setdefault("upper_message_id", message["message_id"])
            if self.add(child, "merge_forward"):
                self.stats["合并转发"] += 1
            if child.get("msg_type") == "merge_forward":
                self.expand_merge_forward(child, depth + 1)

    def download_all(self) -> None:
        downloaded = {p.name for p in self.files_dir.iterdir() if p.is_file() and not p.name.endswith(".part")}
        for message in sorted(self.messages.values(), key=lambda m: int(m.get("create_time") or 0)):
            if message.get("deleted"):
                continue
            for file_key, kind, name in resources_of(message):
                mid = message["message_id"]
                prefix = safe_name(f"{mid}_{name}")
                if any(f.startswith(prefix) for f in downloaded):
                    self.stats["附件已存在"] += 1
                    continue
                try:
                    saved = self.client.download(mid, file_key, kind, self.files_dir, name)
                    downloaded.add(saved.name)
                    self.stats["附件"] += 1
                    print(f"  ↓ {saved.name}")
                except (FeishuError, urllib.error.URLError, TimeoutError, OSError) as e:
                    self.log_error(step="download", message_id=mid, file_key=file_key, kind=kind, error=str(e))

    def write_timeline(self) -> None:
        def sender_name(m: dict) -> str:
            sender = m.get("sender") or {}
            if sender.get("sender_type") == "app":
                return "机器人"
            if m.get("msg_type") == "system" and not sender.get("id"):
                return "系统"
            sid = sender.get("id", "")
            return self.names.get(sid) or sid or "未知"

        def fmt_time(m: dict) -> str:
            ms = int(m.get("create_time") or 0)
            return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else "?"

        files = sorted(p.name for p in self.files_dir.iterdir() if p.is_file())
        children: dict[str, list[dict]] = {}
        top: list[dict] = []
        for m in self.messages.values():
            if m.get("upper_message_id") and m.get("_source") == "merge_forward":
                children.setdefault(m["upper_message_id"], []).append(m)
            else:
                top.append(m)

        lines = [f"# {self.chat.get('name') or self.chat_id}", "",
                 f"导出时间：{datetime.now():%Y-%m-%d %H:%M}，共 {len(self.messages)} 条（含话题回复与合并转发子消息）", ""]

        def render(m: dict, indent: str) -> None:
            flags = []
            if m.get("_source") == "thread":
                flags.append("话题回复")
            if m.get("parent_id"):
                flags.append(f"回复 {m['parent_id'][-8:]}")
            flag = f"（{'，'.join(flags)}）" if flags else ""
            text = readable_text(m, self.names).replace("\n", "\n" + indent + "    ")
            lines.append(f"{indent}- **{fmt_time(m)} {sender_name(m)}**{flag} `{m['message_id'][-8:]}`")
            lines.append(f"{indent}    {text}")
            attached = [f for f in files if f.startswith(m["message_id"] + "_")]
            for f in attached:
                lines.append(f"{indent}    附件：files/{f}")
            for child in sorted(children.get(m["message_id"], []), key=lambda c: int(c.get("create_time") or 0)):
                render(child, indent + "    ")

        for m in sorted(top, key=lambda x: int(x.get("create_time") or 0)):
            render(m, "")
        (self.dir / "messages.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self) -> None:
        print(f"\n== 导出群：{self.chat.get('name')}（{self.chat_id}）→ {self.dir}")
        if self.messages:
            print(f"  已有 {len(self.messages)} 条，只追加新消息")
        self.load_members()
        items = self.fetch_container("chat", self.chat_id, "chat")
        limit = self.cfg["max_messages"]
        print(f"  群消息 {len(items)} 条" + (f"（测试上限 {limit}，从最早的消息开始）" if limit else ""))

        thread_ids = sorted({m["thread_id"] for m in items if m.get("thread_id")})
        for i, thread_id in enumerate(thread_ids, 1):
            try:
                self.fetch_container("thread", thread_id, "thread")
                self.stats["话题"] += 1
            except FeishuError as e:
                self.log_error(step="thread", thread_id=thread_id, error=str(e))
            if i % 20 == 0:
                print(f"  话题 {i}/{len(thread_ids)}")

        for m in [m for m in list(self.messages.values()) if m.get("msg_type") == "merge_forward"]:
            self.expand_merge_forward(m)

        if self.cfg["download_files"]:
            self.download_all()
        self.write_timeline()
        print("  完成：" + "，".join(f"{k} {v}" for k, v in self.stats.items()))
        if self.stats["错误"]:
            print(f"  ⚠ 有 {self.stats['错误']} 条错误，详见 {self.errors_file}")


def main() -> int:
    env = load_env()
    app_id, app_secret = env.get("FEISHU_APP_ID"), env.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("❌ 请先在根目录 .env 填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET（模板见 .env.example 的“飞书导出”段）")
        return 1
    out_root = Path(env.get("FEISHU_EXPORT_DIR") or ROOT / "data" / "feishu_export")
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    cfg = {
        "start_time": to_timestamp(env.get("FEISHU_START_DATE")),
        "end_time": to_timestamp(env.get("FEISHU_END_DATE")),
        "download_files": (env.get("FEISHU_DOWNLOAD_FILES") or "true").lower() not in ("false", "0", "no"),
        "max_messages": int(env.get("FEISHU_MAX_MESSAGES") or 0) or None,
    }
    client = FeishuClient(env.get("FEISHU_DOMAIN") or "https://open.feishu.cn", app_id, app_secret)

    try:
        chats = client.paginate("/open-apis/im/v1/chats", {"page_size": 100})
    except FeishuError as e:
        print(f"❌ 获取机器人所在群失败：{e}")
        print("   检查：App ID/Secret 是否正确、应用是否开启机器人能力、权限是否已开通并发布版本。")
        return 1

    print(f"机器人所在的群（{len(chats)} 个）：")
    for c in chats:
        print(f"  - {c.get('name')}  {c['chat_id']}")
    wanted_ids = {x.strip() for x in (env.get("FEISHU_CHAT_IDS") or "").split(",") if x.strip()}
    keyword = (env.get("FEISHU_CHAT_NAME_KEYWORD") or "").strip()
    if wanted_ids:
        chats = [c for c in chats if c["chat_id"] in wanted_ids]
    if keyword:
        chats = [c for c in chats if keyword in (c.get("name") or "")]
    if not chats:
        print("❌ 没有要导出的群：请把机器人拉进售后群，或检查 FEISHU_CHAT_IDS / FEISHU_CHAT_NAME_KEYWORD")
        return 1

    failed = 0
    for chat in chats:
        try:
            ChatExporter(client, chat, out_root, cfg).run()
        except FeishuError as e:
            failed += 1
            print(f"❌ 导出失败：{e}")
            print("   多半是缺少“获取群组中所有消息”或“读取单聊、群组消息”权限，开通后需重新发布应用版本。")
    print(f"\n导出目录：{out_root}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
