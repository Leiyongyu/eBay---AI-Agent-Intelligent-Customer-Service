"""飞书售后群工单。

划分（规则判定，不交给模型临场决定）：
- 发售后群：质量问题（quality_issue）、破损/错发/缺件（damaged_wrong）、安装技术问题（install_help）、
  买家发来 VIN 核对适配（fitment + 来信里有 VIN）；
- 其余（知识库能答、查系统能答、物流、退货退款取消改地址议价等客服自己定的）不发群。
AI 识别到该发的，起草一张“待发送”工单（资料不全时列出还缺什么），客服在工作台核对后点“发到售后群”；
开了 FEISHU_CASE_AUTO_SEND 时资料齐全的直接发群（缺资料的等买家补齐后自动发）。

卡片格式：标题“eBay <站点>售后/售前 · <原 SKU> · <订单号>”（有订单 = 售后，没订单 = 售前）；正文只有问题（中文）、
故障码 / VIN、买家原文；买家图片发在卡片话题里。不显示店铺。

群里的回复：回在工单话题里（或引用回复卡片），或在群里 @ 机器人（带订单号对到那一单，不带对到最近一张还没回复的工单）。
收到后 AI 按回复起草买家回复（只写群里明确说的，没答的写正在核实），**一律人工审核后才发**；发出后在话题里回帖“已回复买家”。
买家之后的追问自动追加到同一话题。目前买家那边是模拟发送。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .. import usage as usage_log
from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import (AftersalesCase, AftersalesFile, AttachmentStatus, AuditEvent, Conversation, Message,
                      MessageAttachment, Store)
from ..prompts import load_prompt
from .client import FeishuClient, FeishuError, message_text

log = logging.getLogger(__name__)
CATEGORIES = {"quality_issue": "质量问题", "damaged_wrong": "破损错发缺件", "install_help": "安装技术问题"}
OPEN = ("DRAFT", "SENT", "REPLIED", "ANSWERED")
SITE_NAMES = {"EBAY_DE": "德国", "EBAY_GB": "英国", "EBAY_US": "美国", "EBAY_FR": "法国", "EBAY_IT": "意大利",
              "EBAY_ES": "西班牙", "EBAY_AT": "奥地利", "EBAY_AU": "澳大利亚", "EBAY_CA": "加拿大", "EBAY_NL": "荷兰"}
VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b")
FAULT = re.compile(r"\b[PCBU][0-9][0-9A-F]{3}\b")
ORDER = re.compile(r"\b\d{2}-\d{5}-\d{5}\b")
_ALIAS_PREFIX = re.compile(r"^[A-Z]+-(DE|US|UK|GB)-(?=[A-Z]+-\d)", re.I)
_ALIAS_SUFFIX = re.compile(r"-AMZUS\d*$", re.I)
MAX_IMAGES = 9
FEISHU_FILE_MAX = 30 * 1024 * 1024  # 飞书单个文件上限 30MB；更大的在话题里说明，去工作台“售后附件”看
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv", ".3gp", ".wmv", ".mpg", ".mpeg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
FILE_SOURCES = {"ATTACHMENT": "售后邮箱", "WETRANSFER": "WeTransfer 链接", "LINK": "网盘链接", "UPLOAD": "上传页"}


class CaseError(ValueError):
    pass


def clean_sku(sku: str | None) -> str | None:
    """原 SKU：去掉平台别名前缀（ZMY-DE- 等）和 -AMZUS3 后缀；二手件的 -YXR/-RXY 保留。"""
    s = _ALIAS_SUFFIX.sub("", _ALIAS_PREFIX.sub("", (sku or "").strip()))
    return s or None


def category_for(intents: list[str], buyer_text: str) -> str | None:
    for code, name in CATEGORIES.items():
        if code in intents:
            return name
    if "fitment" in intents and VIN.search(buyer_text or ""):
        return "适配核对"
    return None


def title(c: AftersalesCase) -> str:
    site = SITE_NAMES.get(c.marketplace_id or "", c.marketplace_id or "")
    parts = [f"eBay {site}{c.case_type}", c.sku or "SKU 未知", c.order_id or "无订单"]
    return " · ".join(parts)


def card(c: AftersalesCase, test: bool = False) -> dict:
    md = lambda t: {"tag": "lark_md", "content": t}  # noqa: E731
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": ("【测试】" if test else "") + title(c)}},
        "elements": [
            {"tag": "div", "text": md("**问题（中文）**\n" + (c.problem_zh or "（未翻译）"))},
            {"tag": "div", "text": md("**故障码 / VIN**\n" + (c.fault_info or "未提供"))},
            {"tag": "hr"},
            {"tag": "div", "text": md("**买家原文**\n" + (c.original_text or ""))},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "处理意见请回在本消息的话题里，或在群里 @ 机器人并写上订单号。"}]},
        ],
    }


# ---------------------------------------------------------------- 起草工单
def _fault_info(text: str) -> str | None:
    codes = sorted(set(FAULT.findall(text or "")))
    vins = sorted(set(VIN.findall(text or "")))
    lines = []
    if codes:
        lines.append("故障码：" + "、".join(codes))
    if vins:
        lines.append("VIN：" + "、".join(vins))
    return "\n".join(lines) or None


def _missing(category: str, images: list, fault: str | None) -> list[str]:
    if category == "质量问题":
        need = [] if images else ["问题视频（优先，WeTransfer 链接 + 订单号）或照片（可直接在 eBay 里发）"]
        if not fault or "故障码" not in fault:
            need.append("诊断仪读出的故障码截图（有的话）")
        if not fault or "VIN" not in fault:
            need.append("车辆 VIN")
        return need
    if category == "破损错发缺件":
        return [] if images else ["外包装和面单照片", "收到的商品和配件照片"]
    return []


def _case_files(db: Session, conversation_id: int, store_id: str | None, order_id: str | None) -> list[AftersalesFile]:
    """买家通过售后邮箱、上传页发来的文件（已存好、扫描通过）：按会话关联，或同店铺同订单号。"""
    conds = [AftersalesFile.conversation_id == conversation_id]
    if order_id and store_id:
        conds.append(and_(AftersalesFile.order_id == order_id, AftersalesFile.store_id == store_id))
    return list(db.scalars(select(AftersalesFile).where(or_(*conds), AftersalesFile.status == "STORED",
                                                        AftersalesFile.source != "GOODCANG_TICKET")
                           .order_by(AftersalesFile.id)))


def _ext(f: AftersalesFile) -> str:
    from ..aftersales_mail import storage

    return storage.ext_of(f.original_name or f.rel_path, f.content_type)


def _media_files(db: Session, conversation_id: int, store_id: str | None, order_id: str | None) -> list[AftersalesFile]:
    """能算“问题照片或视频”的文件。"""
    return [f for f in _case_files(db, conversation_id, store_id, order_id) if _ext(f) in VIDEO_EXT | IMAGE_EXT]


def _send_files(db: Session, c: AftersalesCase, client: FeishuClient, header: str | None = None) -> int:
    """把还没发过的售后附件发到工单话题：图片发图，视频和其他文件发文件（飞书里可直接播放 / 下载），
    超过 30MB 的说明大小，去工作台看。返回发出的个数。"""
    every = _case_files(db, c.conversation_id, c.store_id, c.order_id)
    sent_hashes = {f.sha256 for f in every if f.id in (c.file_ids or []) and f.sha256}
    files, dup = [], []
    for f in every:  # 同一个视频可能邮箱、eBay 消息链接各下载一次：内容相同的只发一次
        if f.id in (c.file_ids or []):
            continue
        (dup if f.sha256 and f.sha256 in sent_hashes else files).append(f)
        if f.sha256:
            sent_hashes.add(f.sha256)
    if dup:
        c.file_ids = [*(c.file_ids or []), *(f.id for f in dup)]
    if not files:
        return 0
    done = list(c.file_ids or [])
    root = get_settings().aftersales_root
    pending_header = [header] if header else []

    def say_header():  # 说明只在真要发东西时发一次：上传失败、下一轮重试时不会重复刷“买家补充了…”
        if pending_header:
            r = client.reply_text(c.message_id, pending_header.pop())
            c.thread_id = c.thread_id or r.get("thread_id")

    for f in files:
        ext, name = _ext(f), (f.original_name or f"附件{f.id}")
        kind = "视频" if ext in VIDEO_EXT else "图片" if ext in IMAGE_EXT else "文件"
        src = FILE_SOURCES.get(f.source, f.source)
        path = root / f.rel_path if f.rel_path else None
        try:
            if path is None or not path.exists():
                continue
            size = path.stat().st_size
            if size > FEISHU_FILE_MAX:
                say_header()
                r = client.reply_text(c.message_id, f"买家通过{src}发来的{kind}「{name}」有 {size / 1024 / 1024:.0f}MB，"
                                                    f"超过飞书 30MB 上限，没法直接发到群里。"
                                                    + (f"可以点原链接看：{f.link_url}（WeTransfer 链接一般几天后过期）；" if f.link_url else "")
                                                    + f"本地已存一份，在工作台“售后附件”里（附件 #{f.id}）。")
            elif kind == "图片":
                key = client.upload_image(name, path.read_bytes(), f.content_type or "image/jpeg")
                say_header()
                r = client.reply(c.message_id, "image", {"image_key": key})
            else:
                # 飞书：按 mp4 上传的必须用“视频”消息（media，群里可直接播放），用 file 发会报 230055 类型不匹配；
                # 其他格式按普通文件上传、发文件消息
                mp4 = ext in (".mp4", ".m4v")
                key = client.upload_file(name, path.read_bytes(), "mp4" if mp4 else "stream")
                say_header()
                r = client.reply(c.message_id, "media" if mp4 else "file", {"file_key": key})
            c.thread_id = c.thread_id or r.get("thread_id")
            done.append(f.id)
        except (FeishuError, OSError) as e:
            log.warning("工单 %s 售后附件 %s 发送失败：%s", c.id, f.id, e)
    sent = len(done) - len(c.file_ids or [])
    c.file_ids = done
    return sent


def _buyer_messages(db: Session, conv: Conversation) -> list[Message]:
    """店铺最后一次回复之后的买家消息（就是这次要处理的问题）。"""
    msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq)).all()
    last_seller = max((m.conversation_seq for m in msgs if m.sender_role == "SELLER"), default=0)
    return [m for m in msgs if m.sender_role == "BUYER" and m.conversation_seq > last_seller] or \
        [m for m in msgs if m.sender_role == "BUYER"][-3:]


def _translate(db: Session, conv: Conversation, msgs: list[Message], provider=None) -> str:
    parts = [m.zh_translation for m in msgs if m.zh_translation]
    if len(parts) == len(msgs) and parts:
        return "\n\n".join(parts)
    from ..providers.chat import get_chat_provider

    llm = provider or get_chat_provider()
    if llm is None:
        return ""
    text = "\n\n".join(m.body or "" for m in msgs)
    name, model = usage_log.translate_model(get_settings())
    started = time.perf_counter()
    try:
        out, usage = llm.translate_with_usage(text, "中文")
    except Exception:  # noqa: BLE001  翻译失败：发群前在工作台补
        return ""
    usage_log.record(db, "translate", provider=name, model=model, usage=usage,
                     duration_ms=int((time.perf_counter() - started) * 1000), store_id=conv.store_id,
                     conversation_id=conv.id)
    return out.strip()


def open_case(db: Session, conv_id: int) -> AftersalesCase | None:
    return db.scalar(select(AftersalesCase).where(AftersalesCase.conversation_id == conv_id,
                                                  AftersalesCase.status.in_(OPEN)).order_by(AftersalesCase.id.desc()).limit(1))


def build(db: Session, conv: Conversation, *, category: str, order: dict | None, origin: str,
          provider=None) -> AftersalesCase:
    """起草（或刷新还没发出的）工单。"""
    msgs = _buyer_messages(db, conv)
    original = "\n\n".join((m.body or "").strip() for m in msgs if (m.body or "").strip())
    all_text = "\n".join(m.body or "" for m in db.scalars(select(Message).where(
        Message.conversation_id == conv.id, Message.sender_role == "BUYER")))
    atts = db.scalars(select(MessageAttachment).where(MessageAttachment.conversation_id == conv.id,
                                                      MessageAttachment.status == AttachmentStatus.STORED)
                      .order_by(MessageAttachment.id.desc()).limit(MAX_IMAGES)).all()
    fault = _fault_info(all_text)
    order_id = (order or {}).get("order_id")
    sku = (order or {}).get("sku") or next((i.get("sku") for i in (order or {}).get("items") or [] if i.get("sku")), None)
    site = conv.marketplace_id if conv.marketplace_id in SITE_NAMES else None
    if site is None:  # eBay 拉来的会话站点常是 UNKNOWN：用订单的站点，再不行用店铺默认站点
        store = db.get(Store, conv.store_id)
        site = (order or {}).get("marketplace_id") or (store.default_marketplace_id if store else None) or conv.marketplace_id
    c = open_case(db, conv.id)
    if c is not None and c.status != "DRAFT":
        return c  # 已发出：新资料由同步任务追加到话题
    if c is None:
        c = AftersalesCase(conversation_id=conv.id, store_id=conv.store_id, origin=origin, status="DRAFT")
        db.add(c)
    c.order_id = order_id or c.order_id
    c.case_type = "售后" if c.order_id else "售前"
    c.buyer_key, c.marketplace_id = conv.buyer_key, site
    c.category, c.sku = category, clean_sku(sku) or c.sku
    c.original_text, c.fault_info = original, fault
    c.problem_zh = _translate(db, conv, msgs, provider) or c.problem_zh or ""
    c.attachment_ids = [a.id for a in reversed(atts)]
    c.missing = _missing(category, [*atts, *_media_files(db, conv.id, conv.store_id, c.order_id)], fault) or None
    db.flush()
    return c


def suggest(db: Session, conv: Conversation | None, state, draft: dict | None) -> AftersalesCase | None:
    """回复流程落库时调用：按划分规则该发售后群的，起草一张待发送工单（不自动发）。"""
    if conv is None or not draft or not get_settings().feishu_case_enabled:
        return None
    buyer_text = "\n".join(state["snapshot"]["buyer_texts"])
    cat = category_for(draft.get("intents") or [], buyer_text)
    if cat is None:
        return None
    order = next((o["data"] for o in state["observations"] if o["tool_name"] == "get_authorized_order_context"
                  and o["status"] == "FOUND" and isinstance(o.get("data"), dict)), None)
    c = build(db, conv, category=cat, order=order, origin="AI")
    if c.status == "DRAFT":
        db.add(AuditEvent(actor="agent", action="AFTERSALES_CASE_SUGGESTED", store_id=conv.store_id,
                          conversation_id=conv.id, detail={"case_id": c.id, "category": cat}))
        auto_send(db, c)
    return c


AUTO_ACTOR = "AI（自动发群）"


def auto_send(db: Session, c: AftersalesCase, client: FeishuClient | None = None) -> bool:
    """FEISHU_CASE_AUTO_SEND：资料齐全（没有“还缺什么”）的工单直接发到售后群，不用人工点。
    缺资料的先留草稿，AI 回复里请买家补；买家补齐后再跑回复流程时刷新工单，齐了就自动发。发送失败留草稿，工作台可重发。"""
    s = get_settings()
    required = [m for m in c.missing or [] if "有的话" not in m]  # “（有的话）”是可选项，不挡自动发
    if not s.feishu_case_auto_send or c.status != "DRAFT" or required or not s.feishu_ready:
        return False
    try:
        with db.begin_nested():
            send(db, c, AUTO_ACTOR, client)
        return True
    except (CaseError, FeishuError) as e:
        log.warning("工单 %s 自动发群失败：%s", c.id, e)
        db.add(AuditEvent(actor=AUTO_ACTOR, action="AFTERSALES_CASE_AUTO_SEND_FAILED", store_id=c.store_id,
                          conversation_id=c.conversation_id, detail={"case_id": c.id, "error": str(e)[:300]}))
        return False


# ---------------------------------------------------------------- 发到群里
def send(db: Session, c: AftersalesCase, actor: str, client: FeishuClient | None = None) -> AftersalesCase:
    from ..media.service import file_path

    s = get_settings()
    if c.status != "DRAFT":
        raise CaseError("这张工单已经发过了")
    if not s.feishu_ready:
        raise CaseError("飞书售后群没有配置好（FEISHU_CASE_ENABLED、FEISHU_APP_ID/SECRET、FEISHU_CASE_CHAT_ID）")
    if not (c.problem_zh or "").strip():
        conv = db.get(Conversation, c.conversation_id)
        c.problem_zh = _translate(db, conv, _buyer_messages(db, conv)) if conv else ""
    client = client or FeishuClient(s)
    c.chat_id = s.feishu_case_chat_id
    c.message_id = client.send_card(c.chat_id, card(c, test=_is_test(db, c)))
    for aid in c.attachment_ids or []:
        att = db.get(MessageAttachment, aid)
        path = file_path(att) if att else None
        if path is None:
            continue
        try:
            key = client.upload_image(path.name, path.read_bytes(), att.content_type or "image/jpeg")
            r = client.reply(c.message_id, "image", {"image_key": key})
            c.thread_id = c.thread_id or r.get("thread_id")
        except FeishuError as e:
            log.warning("工单 %s 附件 %s 发送失败：%s", c.id, aid, e)
    _send_files(db, c, client)  # 售后邮箱、上传页来的视频和图片
    if not c.thread_id:  # 没有图片：发一句话把话题建起来，方便大家回在话题里
        c.thread_id = client.reply_text(c.message_id, "处理意见请回在这个话题里。").get("thread_id")
    conv = db.get(Conversation, c.conversation_id)
    c.last_buyer_seq = max((m.conversation_seq for m in _buyer_messages(db, conv)), default=0) if conv else 0
    c.status, c.sent_by, c.sent_at = "SENT", actor, utcnow()
    db.add(AuditEvent(actor=actor, action="AFTERSALES_CASE_SENT", store_id=c.store_id, conversation_id=c.conversation_id,
                      detail={"case_id": c.id, "message_id": c.message_id, "images": len(c.attachment_ids or [])}))
    return c


def _is_test(db: Session, c: AftersalesCase) -> bool:
    conv = db.get(Conversation, c.conversation_id)
    return conv is None or conv.channel_source is None


# ---------------------------------------------------------------- 读群里的回复
class CaseReply(BaseModel):
    language: str = Field(default="", max_length=16)
    reply_text: str = Field(max_length=3000)
    reply_zh: str = Field(default="", max_length=3000)
    unanswered: list[str] = Field(default_factory=list)


def _save_reply_image(db: Session, c: AftersalesCase, client: FeishuClient, message_id: str, key: str, i: int) -> int | None:
    from ..aftersales_mail import storage

    try:
        data = client.download_image(message_id, key)
    except FeishuError:
        return None
    store = db.get(Store, c.store_id)
    saved = storage.save_stream([data], storage.store_dir(c.store_id, store.ebay_user_id if store else None),
                                storage.file_name(c.order_id, utcnow(), i, ".jpg"), "image/jpeg")
    f = AftersalesFile(mail_id=None, store_id=c.store_id, order_id=c.order_id, conversation_id=c.conversation_id,
                       source="FEISHU_CASE", original_name=f"群回复图片{i}.jpg", size_bytes=saved.size,
                       content_type=saved.content_type, sha256=saved.sha256, rel_path=saved.rel_path, status="STORED",
                       status_reason=f"飞书售后群工单 {c.id} 里同事回复的图片")
    db.add(f)
    db.flush()
    return f.id


def _add_reply(db: Session, c: AftersalesCase, m: dict, names: dict, client: FeishuClient, via: str) -> bool:
    text, images = message_text(m)
    if not text and not images:
        return False
    files = [fid for i, k in enumerate(images, 1) if (fid := _save_reply_image(db, c, client, m["message_id"], k, i))]
    sender = (m.get("sender") or {}).get("id")
    at = datetime.fromtimestamp(int(m.get("create_time") or 0) / 1000, tz=timezone.utc).replace(tzinfo=None)
    c.replies = (c.replies or []) + [{"message_id": m["message_id"], "sender": names.get(sender, sender), "text": text,
                                      "file_ids": files, "at": at.isoformat(timespec="seconds") + "Z", "via": via}]
    return True


def _known_ids(db: Session) -> set[str]:
    ids = set()
    for c in db.scalars(select(AftersalesCase).where(AftersalesCase.replies.is_not(None))):
        ids |= {r.get("message_id") for r in c.replies or []}
    return ids


def poll(db: Session, client: FeishuClient | None = None, provider=None) -> dict:
    """读一次群里的回复（话题 + @ 机器人），有新回复的起草买家回复；再把买家的追问追加到话题。"""
    s = get_settings()
    client = client or FeishuClient(s)
    stats = {"replies": 0, "drafted": 0, "followups": 0, "files": 0, "released": refresh_waiting(db, client)}
    cases = db.scalars(select(AftersalesCase).where(AftersalesCase.status.in_(("SENT", "REPLIED", "ANSWERED")),
                                                    AftersalesCase.message_id.is_not(None))).all()
    if not cases:
        return stats
    known = _known_ids(db)
    names: dict = {}
    updated: set[int] = set()
    for chat in {c.chat_id for c in cases if c.chat_id}:
        try:
            names.update(client.members(chat))
        except FeishuError:
            pass
    # ① 工单话题里的回复（含引用回复卡片）
    for c in cases:
        if not c.thread_id:
            continue
        for m in client.messages("thread", c.thread_id):
            if m["message_id"] in known or (m.get("sender") or {}).get("sender_type") == "app" or m["message_id"] == c.message_id:
                continue
            if _add_reply(db, c, m, names, client, "thread"):
                known.add(m["message_id"])
                updated.add(c.id)
                stats["replies"] += 1
    # ② 群里 @ 机器人：带订单号对到那一单，不带对到最近一张还没回复的工单
    bot = client.bot_open_id()
    since = int(min(c.sent_at for c in cases if c.sent_at).replace(tzinfo=timezone.utc).timestamp()) - 60
    for chat in {c.chat_id for c in cases if c.chat_id}:
        for m in client.messages("chat", chat, start_time=since):
            if m["message_id"] in known or m.get("thread_id") or (m.get("sender") or {}).get("sender_type") == "app":
                continue
            mentioned = any(x.get("id") == bot for x in m.get("mentions") or []) if bot else False
            parent = m.get("parent_id") or m.get("root_id")
            target = next((c for c in cases if parent and parent == c.message_id), None)
            if target is None and mentioned:
                text, _ = message_text(m)
                order = next(iter(ORDER.findall(text)), None)
                pool = [c for c in cases if c.chat_id == chat]
                if order:
                    target = next((c for c in pool if c.order_id == order), None)
                else:  # 不带订单号：最近一张还没回复的工单；都回复过时，群里只有一张在跟的才算它（拿不准不乱对）
                    target = max((c for c in pool if not c.replies), key=lambda c: c.sent_at, default=None) or \
                        (pool[0] if len(pool) == 1 else None)
            if target is not None and _add_reply(db, target, m, names, client, "mention" if mentioned else "quote"):
                known.add(m["message_id"])
                updated.add(target.id)
                stats["replies"] += 1
    db.flush()
    for c in cases:
        if c.id in updated:
            draft_reply(db, c, provider)
            stats["drafted"] += 1
        stats["followups"] += forward_followups(db, c, client)
        try:  # 买家后来通过售后邮箱、上传页补的视频和图片
            stats["files"] += _send_files(db, c, client, header="买家补充了视频 / 图片：")
        except FeishuError as e:
            log.warning("工单 %s 追加售后附件失败：%s", c.id, e)
    return stats


def refresh_waiting(db: Session, client: FeishuClient | None = None) -> int:
    """因为缺照片 / 视频还没发群的工单：买家后来通过售后邮箱、上传页发来了，就补上资料；齐了且开了自动发送就发群。"""
    n = 0
    for c in db.scalars(select(AftersalesCase).where(AftersalesCase.status == "DRAFT")).all():
        if not any("照片" in m or "视频" in m for m in c.missing or []):
            continue
        files = _media_files(db, c.conversation_id, c.store_id, c.order_id)
        if not files:
            continue
        c.missing = _missing(c.category, [*(c.attachment_ids or []), *files], c.fault_info) or None
        n += auto_send(db, c, client)
    return n


def draft_reply(db: Session, c: AftersalesCase, provider=None) -> None:
    """按群里的回复起草买家回复（买家语言 + 中文对照），状态改为“群已回复待审核”。不发送。"""
    from ..providers.chat import get_chat_provider

    llm = provider or get_chat_provider()
    conv = db.get(Conversation, c.conversation_id)
    msgs = db.scalars(select(Message).where(Message.conversation_id == c.conversation_id)
                      .order_by(Message.conversation_seq.desc()).limit(12)).all()[::-1]
    history = "\n".join(f"[{m.sender_role}] {(m.body or '')[:800]}" for m in msgs)
    replies = "\n".join(f"- {r.get('sender')}：{r.get('text') or '（图片）'}" for r in c.replies or [])
    case_text = f"类别：{c.category}\n问题（中文）：{c.problem_zh}\n故障码/VIN：{c.fault_info or '未提供'}\n买家原文：{c.original_text}"
    c.status = "REPLIED"
    if llm is None:
        return
    prompt = [SystemMessage(content=load_prompt("feishu_case_reply")),
              HumanMessage(content=f"<conversation>\n{history}\n</conversation>\n<case>\n{case_text}\n</case>\n"
                                   f"<team_replies>\n{replies}\n</team_replies>\n<site>{conv.marketplace_id if conv else ''}</site>")]
    name, model = usage_log.chat_model(get_settings())
    started = time.perf_counter()
    r = llm.structured(prompt, CaseReply)
    usage_log.record(db, "case_reply", provider=name, model=model, usage=r.usage, ok=r.parsed is not None, error=r.error,
                     duration_ms=int((time.perf_counter() - started) * 1000), store_id=c.store_id,
                     conversation_id=c.conversation_id)
    if r.parsed:
        c.draft_reply, c.draft_reply_zh, c.drafted_at = r.parsed.reply_text.strip(), r.parsed.reply_zh.strip(), utcnow()


def forward_followups(db: Session, c: AftersalesCase, client: FeishuClient) -> int:
    """发群之后买家又来的消息：追加到工单话题（原文 + 中文 + 图片），一单一个话题。"""
    from ..media.service import file_path

    if c.status not in ("SENT", "REPLIED", "ANSWERED") or not c.message_id:
        return 0
    new = db.scalars(select(Message).where(Message.conversation_id == c.conversation_id, Message.sender_role == "BUYER",
                                           Message.conversation_seq > (c.last_buyer_seq or 0))
                     .order_by(Message.conversation_seq)).all()
    n = 0
    for m in new:
        if m.zh_translation is None and (m.body or "").strip() and m.sent_at and m.sent_at > utcnow() - timedelta(minutes=10):
            break  # 刚到的消息等自动翻译出来再追加（最多等 10 分钟）
        text = f"买家追问：\n{(m.body or '').strip()}" + (f"\n中文：{m.zh_translation}" if m.zh_translation else "")
        try:
            if (m.body or "").strip():
                client.reply_text(c.message_id, text)
            for att in db.scalars(select(MessageAttachment).where(MessageAttachment.message_id == m.id,
                                                                  MessageAttachment.status == AttachmentStatus.STORED)):
                path = file_path(att)
                if path:
                    client.reply(c.message_id, "image", {"image_key": client.upload_image(path.name, path.read_bytes())})
        except FeishuError as e:
            log.warning("工单 %s 追加买家追问失败：%s", c.id, e)
            break
        c.last_buyer_seq = m.conversation_seq
        if c.status == "ANSWERED":
            c.status = "SENT"  # 买家又追问了：等群里回复
        n += 1
    return n


def after_seller_reply(db: Session, conv: Conversation, text: str, text_zh: str | None, actor: str) -> AftersalesCase | None:
    """客服在工作台给买家发出回复后：群已回复的工单标“已回复买家”，并在话题里回帖。"""
    c = db.scalar(select(AftersalesCase).where(AftersalesCase.conversation_id == conv.id,
                                               AftersalesCase.status == "REPLIED").order_by(AftersalesCase.id.desc()).limit(1))
    if c is None:
        return None
    c.status, c.answered_at = "ANSWERED", utcnow()
    db.add(AuditEvent(actor=actor, action="AFTERSALES_CASE_ANSWERED", store_id=c.store_id, conversation_id=conv.id,
                      detail={"case_id": c.id}))
    if get_settings().feishu_ready and c.message_id:
        try:
            FeishuClient().reply_text(c.message_id, f"已回复买家（{actor}）：\n{text_zh or text}")
        except FeishuError as e:
            log.warning("工单 %s 回帖失败：%s", c.id, e)
    return c


def close(db: Session, c: AftersalesCase, actor: str) -> AftersalesCase:
    if c.status == "CLOSED":
        raise CaseError("工单已经完结")
    c.status, c.closed_by, c.closed_at = "CLOSED", actor, utcnow()
    db.add(AuditEvent(actor=actor, action="AFTERSALES_CASE_CLOSED", store_id=c.store_id, conversation_id=c.conversation_id,
                      detail={"case_id": c.id}))
    if get_settings().feishu_ready and c.message_id:
        try:
            FeishuClient().reply_text(c.message_id, f"工单已完结（{actor}）。")
        except FeishuError:
            pass
    return c


# ---------------------------------------------------------------- 定时读取
class CaseWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.last_at: datetime | None = None

    def start(self) -> bool:
        if not get_settings().feishu_ready:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="feishu-cases", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_now(self) -> dict:
        with self._lock:
            try:
                with session_scope() as db:
                    self.last = poll(db)
            except FeishuError as e:
                self.last = {"error": str(e)}
            self.last_at = utcnow()
            return self.last

    def _loop(self) -> None:
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.run_now()
            except Exception:  # noqa: BLE001
                log.exception("读取飞书售后群回复出错")
            self._stop.wait(max(30, get_settings().feishu_case_poll_seconds))


worker = CaseWorker()
