"""客户测试消息系统：模拟 eBay 买家给智能客服发消息。在 PyCharm 里右键 Run 'main'（Shift+F10）即可。

独立运行，不需要先启动主程序，也不调用主程序的任何接口：
- 直接连主程序同一个数据库（读根目录 .env 的 DATABASE_URL 等），复用主程序的接入代码写入买家消息，
  所以消息去重、合并窗口、回复任务与真实来信完全一样；主程序的 Worker 运行时会自动认领并生成回复。
- 主程序没在运行时，消息先排队（任务状态“排队中”），主程序启动后自动处理。
- 只在 .env 为 EBAY_CHANNEL=mock 时允许发送：此时店铺回复只写本地数据库，绝不会发到 eBay。
- 测试用户（买家身份）和会话记录保存在本目录 data/test_users.json；删用户只删本地记录，不删数据库里的会话。
- 商品链接粘贴 eBay 刊登地址即可（如 https://www.ebay.de/itm/123456789012），发送前会实时核对是不是所选店铺的刊登。
- 本程序默认端口 8100，被占用时顺延。可选反向开关：--no-browser（不自动打开浏览器）
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
VENV = PROJECT_ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

# 用了别的解释器（如全局 Python 3.14，装的 SQLAlchemy 版本不兼容）时，自动换项目 .venv 重新运行：
# 主程序的依赖都按 requirements.txt 装在 .venv 里
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV.resolve():
    print(f"当前解释器不是项目虚拟环境（{sys.executable}），自动改用 {VENV_PYTHON} 运行", flush=True)
    try:
        sys.exit(subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]))
    except KeyboardInterrupt:
        sys.exit(0)

sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, Field, model_validator  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.ingestion.service import get_or_create_conversation, ingest_buyer_message  # noqa: E402
from app.media.service import add_uploaded_image, attachments_for_messages  # noqa: E402
from app.media.service import file_path as media_file_path  # noqa: E402
from app.models import AgentRun, Conversation, Draft, JobState, Message, MessageAttachment, ReplyJob, SendIntent, Store  # noqa: E402
from app.sending.service import AUTO_ACTOR  # noqa: E402
from app.sources import SourceNotAvailable, get_source_for_store  # noqa: E402

DATA_FILE = HERE / "data" / "test_users.json"
STATIC = HERE / "static"
PORT = 8100
ACTOR = "客户测试消息系统"

log = logging.getLogger("customer-sim")

# eBay 站点域名 → 站点；带商品时真正的站点以查到的刊登为准，这里只作提示
DOMAIN_MARKETPLACE = {"ebay.com": "EBAY_US", "ebay.de": "EBAY_DE", "ebay.co.uk": "EBAY_GB", "ebay.com.au": "EBAY_AU",
                      "ebay.fr": "EBAY_FR", "ebay.it": "EBAY_IT", "ebay.es": "EBAY_ES", "ebay.ca": "EBAY_CA",
                      "ebay.at": "EBAY_AT"}
ITEM_ID_PATTERNS = (re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,15})"), re.compile(r"[?&](?:item|itemId|iid)=(\d{9,15})"))


# ------------------------------------------------------------------ 本地存储（测试用户与会话）
class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"users": [], "sessions": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


local = JsonStore(DATA_FILE)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def parse_item_link(text_: str) -> dict:
    """从 eBay 商品链接（或纯刊登号）解析刊登号和站点。"""
    text_ = (text_ or "").strip()
    if not text_:
        return {"item_id": None, "marketplace_id": None}
    if re.fullmatch(r"\d{9,15}", text_):
        return {"item_id": text_, "marketplace_id": None}
    url = text_ if "://" in text_ else f"https://{text_}"
    item_id = next((m.group(1) for p in ITEM_ID_PATTERNS if (m := p.search(url))), None)
    if not item_id:
        raise HTTPException(422, "没从链接里找到刊登号：请粘贴 eBay 商品页地址（含 /itm/数字），或直接填刊登号")
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return {"item_id": item_id, "marketplace_id": DOMAIN_MARKETPLACE.get(host)}


def lookup_listing(store_id: str, item_id: str) -> dict | None:
    """真实店铺实时查本店刊登（只读 GetItem，并核对卖家是本店），演示店铺查演示数据。"""
    try:
        return get_source_for_store(store_id).get_listing(store_id, item_id)
    except SourceNotAvailable as e:
        raise HTTPException(502, f"查询刊登失败：{e}") from None


def require_mock() -> None:
    if get_settings().ebay_channel != "mock":
        raise HTTPException(403, "根目录 .env 不是 EBAY_CHANNEL=mock：为免测试回复发到真实 eBay，已禁止发送")


app = FastAPI(title="客户测试消息系统")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    s = get_settings()
    out = {"database": s.database_display, "db_ok": False, "channel": s.ebay_channel,
           "send_enabled": s.ebay_channel == "mock", "error": None}
    try:
        with session_scope() as db:
            db.execute(text("SELECT 1"))
        out["db_ok"] = True
    except Exception as e:
        out["error"] = f"连接数据库失败：{str(getattr(e, 'orig', e)).strip().splitlines()[0]}"
        return out
    if not out["send_enabled"]:
        out["error"] = f".env 是 EBAY_CHANNEL={s.ebay_channel}，为免回复发到真实 eBay，已禁止发送"
    return out


@app.get("/api/stores")
def stores():
    with session_scope() as db:
        rows = sorted(db.scalars(select(Store)), key=lambda s: (s.ebay_user_id is None, s.name.lower()))
        return [{"id": s.id, "name": s.name, "default_marketplace_id": s.default_marketplace_id,
                 "is_ebay": bool(s.ebay_user_id), "auto_send_enabled": s.auto_send_enabled} for s in rows]


# ------------------------------------------------------------------ 测试用户
class UserBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    buyer_key: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=200)


@app.get("/api/users")
def list_users():
    data = local.load()
    counts: dict[str, int] = {}
    for s in data["sessions"]:
        counts[s["user_id"]] = counts.get(s["user_id"], 0) + 1
    return [{**u, "sessions": counts.get(u["id"], 0)} for u in data["users"]]


@app.post("/api/users")
def create_user(body: UserBody):
    buyer_key = (body.buyer_key or "").strip()
    if not buyer_key:
        # 买家账号名：按名字生成英文小写串，中文名就只用随机后缀；sim_ 前缀方便在工作台认出测试会话
        slug = re.sub(r"[^a-z0-9]+", "_", body.name.lower()).strip("_")[:24]
        buyer_key = f"sim_{slug + '_' if slug else ''}{secrets.token_hex(2)}"
    if not re.fullmatch(r"[\w.\-]{1,100}", buyer_key):
        raise HTTPException(422, "买家账号名只能用字母、数字、下划线、点和短横线")
    with local.lock:
        data = local.load()
        if any(u["buyer_key"] == buyer_key for u in data["users"]):
            raise HTTPException(409, f"买家账号名 {buyer_key} 已被其他测试用户使用")
        user = {"id": f"u_{secrets.token_hex(4)}", "name": body.name.strip(), "buyer_key": buyer_key,
                "note": (body.note or "").strip(), "created_at": now()}
        data["users"].append(user)
        local.save(data)
    return user


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str):
    with local.lock:
        data = local.load()
        data["users"] = [u for u in data["users"] if u["id"] != user_id]
        data["sessions"] = [s for s in data["sessions"] if s["user_id"] != user_id]
        local.save(data)
    return {"ok": True}


# ------------------------------------------------------------------ 会话与发消息
@app.get("/api/sessions")
def list_sessions(user_id: str):
    sessions = [s for s in local.load()["sessions"] if s["user_id"] == user_id]
    return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    with local.lock:
        data = local.load()
        data["sessions"] = [s for s in data["sessions"] if s["id"] != session_id]
        local.save(data)
    return {"ok": True}


class ParseBody(BaseModel):
    url: str = Field(max_length=2000)


@app.post("/api/parse-link")
def parse_link(body: ParseBody):
    return parse_item_link(body.url)


class ImageBody(BaseModel):
    name: str | None = Field(default=None, max_length=300)
    data_base64: str = Field(max_length=14_000_000, description="图片 base64，可带 data:image/...;base64, 前缀")


class SendBody(BaseModel):
    user_id: str
    store_id: str | None = None  # 新会话必填；已有会话按会话记录的店铺发
    item_url: str | None = Field(default=None, max_length=2000)
    session_id: str | None = None
    text: str = Field(default="", max_length=5000)
    images: list[ImageBody] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _text_or_images(self):
        if not self.text.strip() and not self.images:
            raise ValueError("消息内容和图片至少要有一项")
        return self


def decode_image(img: ImageBody) -> bytes:
    raw = img.data_base64.split(",", 1)[1] if img.data_base64.startswith("data:") else img.data_base64
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, f"图片 {img.name or ''} 不是有效的 base64") from None


@app.post("/api/send")
def send(body: SendBody):
    require_mock()
    data = local.load()
    user = next((u for u in data["users"] if u["id"] == body.user_id), None)
    if user is None:
        raise HTTPException(404, "测试用户不存在")
    session = next((s for s in data["sessions"] if s["id"] == body.session_id), None) if body.session_id else None
    images = [(img.name, decode_image(img)) for img in body.images]
    listing = None
    if session:
        store_id, item_id, marketplace = session["store_id"], session["item_id"], session["marketplace_id"]
    else:
        if not body.store_id:
            raise HTTPException(422, "请先选择店铺")
        link = parse_item_link(body.item_url or "")
        store_id, item_id = body.store_id, link["item_id"]
        if item_id:
            # 先查刊登再开事务：eBay 查询可能要几秒
            listing = lookup_listing(store_id, item_id)
            if listing is None:
                raise HTTPException(422, "该刊登不属于所选店铺或已下架")

    with session_scope() as db:
        if session:
            conv = db.get(Conversation, session["conversation_id"])
            if conv is None:
                raise HTTPException(404, "数据库里找不到这个会话（可能已被清理），请新开会话")
        else:
            store = db.get(Store, store_id)
            if store is None:
                raise HTTPException(404, "店铺不存在")
            # 站点优先取自刊登，不按语言或买家自述推断（第 11.3 节）；不带商品时用店铺默认站点
            if listing:
                marketplace, source = listing.get("marketplace_id") or link["marketplace_id"] or "UNKNOWN", "listing"
            else:
                marketplace, source = store.default_marketplace_id or "UNKNOWN", "simulator"
            conv_key = f"SIM-{store_id}-{user['buyer_key']}-{item_id or 'general'}-{marketplace}"
            conv = get_or_create_conversation(db, store_id, conv_key, user["buyer_key"], item_id, marketplace, source)
        if conv.channel_source == "ebay":
            raise HTTPException(409, "不能往从 eBay 拉取的真实会话里注入测试消息")
        result = ingest_buyer_message(db, conv=conv, ebay_message_id=f"SIM-IN-{uuid.uuid4().hex[:12]}",
                                      body=body.text, skip_merge_window=True, actor=ACTOR)
        # 图片走与 eBay 附件相同的校验、去元数据、重新编码流程
        stored = [add_uploaded_image(db, result.message, i, name, raw) for i, (name, raw) in enumerate(images, 1)]
        out = {"conversation_id": conv.id, "job_id": result.job.id if result.job else None,
               "cancelled_job_ids": list(result.cancelled_job_ids),
               "attachments": [{"id": a.id, "status": a.status, "reason": a.status_reason} for a in stored]}
        marketplace = conv.marketplace_id

    with local.lock:
        data = local.load()
        # 同一用户 + 店铺 + 刊登在数据库里就是同一个会话，本地也只留一条记录
        session = next((s for s in data["sessions"]
                        if s["user_id"] == user["id"] and s["conversation_id"] == out["conversation_id"]), None)
        if session is None:
            session = {"id": f"s_{secrets.token_hex(4)}", "user_id": user["id"],
                       "conversation_id": out["conversation_id"], "store_id": store_id, "item_id": item_id,
                       "item_url": (body.item_url or "").strip() or None, "marketplace_id": marketplace,
                       "title": listing.get("title") if listing else None, "created_at": now()}
            data["sessions"].append(session)
        session["updated_at"] = now()
        session["last_text"] = body.text.strip()[:80] or "[图片]"
        local.save(data)
    return {"session": session, **out}


@app.get("/api/conversations/{conv_id}")
def conversation(conv_id: int):
    with session_scope() as db:
        conv = db.get(Conversation, conv_id)
        if conv is None:
            raise HTTPException(404, "会话不存在")
        messages = db.scalars(select(Message).where(Message.conversation_id == conv.id)
                              .order_by(Message.conversation_seq)).all()
        atts: dict[int, list] = {}
        for a in attachments_for_messages(db, [m.id for m in messages]):
            atts.setdefault(a.message_id, []).append({"id": a.id, "status": a.status})
        jobs = db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv.id).order_by(ReplyJob.id.desc())).all()
        job = next((j for j in jobs if j.state != JobState.CANCELLED_STALE), None)
        sent_from = {}  # 平台消息 ID → 是否 AI 草稿、是否自动发送
        if jobs:
            for intent, author in db.execute(
                    select(SendIntent, Draft.author).join(Draft, Draft.id == SendIntent.draft_id)
                    .where(SendIntent.job_id.in_([j.id for j in jobs]), SendIntent.platform_message_id.is_not(None))):
                sent_from[intent.platform_message_id] = {"from_ai": author == "agent", "auto": intent.actor == AUTO_ACTOR}
        draft = run = None
        if job:
            draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == job.run_generation,
                                                  Draft.author == "agent").order_by(Draft.id.desc()).limit(1))
            run = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id,
                                                   AgentRun.run_generation == job.run_generation))
        return {
            "conversation": {"id": conv.id, "store_id": conv.store_id, "buyer_key": conv.buyer_key,
                             "item_id": conv.item_id, "marketplace_id": conv.marketplace_id,
                             "handoff_state": conv.handoff_state},
            "messages": [{"id": m.id, "role": m.sender_role, "direction": m.direction, "body": m.body,
                          "sent_at": iso(m.sent_at), "trace": sent_from.get(m.ebay_message_id),
                          "attachments": atts.get(m.id, [])} for m in messages],
            "job": {"id": job.id, "state": job.state, "decision_reasons": job.decision_reasons or [],
                    "last_error": job.last_error, "created_at": iso(job.created_at)} if job else None,
            "run": {"state": run.state, "error": run.error} if run else None,
            "agent_draft": {"text": draft.text, "text_zh": draft.text_zh, "intent": draft.intent,
                            "decision": draft.decision, "risk_level": draft.risk_level,
                            "review_reasons": draft.review_reasons or [], "status": draft.status} if draft else None,
        }


class ListingMemo(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    marketplace_id: str | None = Field(default=None, max_length=32)


@app.get("/api/sessions/{session_id}/listing")
def session_listing(session_id: str):
    """会话商品卡片：实时查本店刊登（进程内缓存 10 分钟），查到后把标题记到本地会话记录。"""
    s = next((x for x in local.load()["sessions"] if x["id"] == session_id), None)
    if s is None or not s.get("item_id"):
        return {"item_id": None}
    card = {"item_id": s["item_id"], "url": s.get("item_url"), "error": None}
    try:
        listing = get_source_for_store(s["store_id"]).get_listing(s["store_id"], s["item_id"])
    except Exception as e:  # 查询出错只影响卡片，不影响聊天
        listing, card["error"] = None, f"查询商品失败：{type(e).__name__}"
    if not listing:
        card["error"] = card["error"] or "商品不属于当前店铺或已下架"
        return card
    card.update({"title": listing.get("title"), "price": listing.get("price"),
                 "picture_url": listing.get("picture_url"), "marketplace_id": listing.get("marketplace_id"),
                 "url": listing.get("url") or card["url"]})
    if card["title"] != s.get("title"):
        with local.lock:
            data = local.load()
            for x in data["sessions"]:
                if x["id"] == session_id:
                    x["title"] = card["title"]
            local.save(data)
    return card


@app.get("/api/attachments/{att_id}/file")
def attachment(att_id: int):
    with session_scope() as db:
        att = db.get(MessageAttachment, att_id)
        path = media_file_path(att) if att else None
        content_type = att.content_type if att else None
    if path is None:
        raise HTTPException(404, "图片不存在或已过保留期")
    return FileResponse(path, media_type=content_type or "application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------------ 启动
def pick_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    sys.exit(f"{start}-{start + 19} 端口都被占用")


def open_browser_when_ready(url: str) -> None:
    for _ in range(40):
        try:
            urllib.request.urlopen(url, timeout=1)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.25)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = get_settings()
    port = pick_port(PORT)
    url = f"http://127.0.0.1:{port}/"
    log.info("客户测试消息系统：%s（数据库：%s，渠道：%s）", url, settings.database_display, settings.ebay_channel)
    if settings.ebay_channel != "mock":
        log.warning("根目录 .env 不是 EBAY_CHANNEL=mock，页面可以打开但禁止发送")
    if "--no-browser" not in sys.argv[1:]:
        threading.Thread(target=open_browser_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
