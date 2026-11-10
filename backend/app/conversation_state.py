"""会话状态卡：让 AI 结合整段会话理解买家在说哪件事（conversations.state_card）。

卡片内容（JSON，控制在约 MAX_TOKENS token 内）：
- topic：当前主题（模型写）；
- issues：未解决问题，每条 {id, source, intent, summary_zh, order_id, since, status, refs, flags}：
  - source=system：程序按库里的事实写，权威、模型不能改——在跟的谷仓查件单、未结 eBay 咨询 / 纠纷 / 退货、
    发票申请、异常包裹跟进、飞书售后工单；每次起草前重新生成，库里结了就消失；
  - source=model：模型在起草调用里顺带输出（state_update.open_issues），程序合并；这次没再列出的视为已解决；
  - flags：给关键词规则用的标记——NOT_RECEIVED 买家说没收到、RESHIP 要求补发、EMPTY_PARCEL 空包裹、
    LOGISTICS 物流问题、INVOICE 发票；
- resolved：最近解决的问题（最多 MAX_RESOLVED 条）；
- facts：关键事实（order_ids、vin、vehicle、skus、media）；order_ids 和 media 由程序补；
- promises：我们答应过买家的事（模型写）。

起草时把卡片作为单独一段放进提示词（聊天记录之前），模型据此判断意图、填 inherits_issue（沿用的问题 ID）；
落库时合并模型的 state_update（系统问题始终以库里为准）。不新增模型调用。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .knowledge.retriever import estimate_tokens
from .models import (AftersalesCase, Conversation, EbayCase, InvoiceRequest, LogisticsTicket, Message, MessageAttachment,
                     ParcelFollowup)

NOT_RECEIVED, RESHIP, EMPTY_PARCEL, LOGISTICS, INVOICE = "NOT_RECEIVED", "RESHIP", "EMPTY_PARCEL", "LOGISTICS", "INVOICE"
FLAG_LABELS = {NOT_RECEIVED: "买家说没收到包裹", RESHIP: "买家要求补发 / 重发", EMPTY_PARCEL: "买家说收到空包裹",
               LOGISTICS: "物流问题", INVOICE: "发票"}
# 意图自带的标记（模型列出的问题按意图补上）
INTENT_FLAGS = {"shipping_status": {LOGISTICS}, "delivery_delay": {LOGISTICS}, "invoice_vat": {INVOICE}}
# 售后类型别名：eBay 实时工具（sources/ebay.py）把未收到货咨询叫 INR，付款纠纷原因里叫 ITEM_NOT_RECEIVED；
# 同步进 ebay_cases 表后才叫 INQUIRY。都按未收到货咨询处理（带 NOT_RECEIVED / LOGISTICS 标记）
CASE_ALIASES = {"INR": "INQUIRY", "ITEM_NOT_RECEIVED": "INQUIRY"}
# 短追问兜底时，各意图对应要接上的标记（只接和模型判断的意图有关的问题，见 carried_flags）
INTENT_RELATED_FLAGS = {"shipping_status": {LOGISTICS, NOT_RECEIVED, RESHIP, EMPTY_PARCEL},
                        "delivery_delay": {LOGISTICS, NOT_RECEIVED, RESHIP, EMPTY_PARCEL},
                        "damaged_wrong": {RESHIP, EMPTY_PARCEL}, "invoice_vat": {INVOICE}}
# 退货退款、退款进度意图：只接 eBay 售后单（sys:case:…）上的标记，不接查件单 / 异常包裹——
# “我开了 case，有进展吗”能接上未收到货咨询的“没收到”，问退款时不会误接别的物流问题
CASE_INTENTS = {"return_refund": {LOGISTICS, NOT_RECEIVED}, "refund_status": {LOGISTICS, NOT_RECEIVED}}
# 短追问：按句子判断，一句不超过 FOLLOW_UP_SENTENCE 字的追问就算（长消息里“有进展吗 + 问适配”也能认出来）
FOLLOW_UP_SENTENCE = 100
# 断句：英文标点后面要跟空白才算句末（“1.6 TDI”“Nr. 5”不会被切开），中文句号问号叹号、换行直接断
# 常见缩写（德英法意西）：句号后面跟空格也不算句末，免得把长句子切出一段带“noch nicht”的短句
ABBREVIATIONS = {
    "z.b", "ca", "bzw", "usw", "evtl", "inkl", "exkl", "nr", "ggf", "d.h", "u.a", "z.t", "o.ä", "u.u", "vgl", "bsp", "max", "min",
    "str", "tel", "zzgl", "mwst", "gem", "e.g", "i.e", "etc", "approx", "no", "vs", "mr", "mrs", "ms", "dr", "st", "incl", "excl",
    "env", "p.ex", "cf", "réf", "ref", "n°", "ecc", "p.es", "pag", "sig", "aprox", "p.ej", "núm", "num", "sr", "sra", "ud", "uds",
}
_SENT_END = re.compile(r"[.!?]\s+")
LEGACY_ORDER = re.compile(r"^\d{9,}-\d{6,}$")  # 旧格式订单号：商品号-交易号


def _spaced_abbreviation(part: str, start: int, m) -> bool:
    """带空格的缩写（z. B. / d. h. / u. a.）：前一个点（“z.”后面跟“B.”）和后一个点（“z. B.”的“B.”）都不断。"""
    before = part[start:m.start()]
    one = re.search(r"(?:^|[\s(（\[])(\w)$", before)
    nxt = re.match(r"(\w)\.", part[m.end():])
    if one and nxt and f"{one.group(1)}.{nxt.group(1)}".lower() in ABBREVIATIONS:
        return True
    two = re.search(r"(?:^|[\s(（\[])(\w)\.\s+(\w)$", before)
    return bool(two and f"{two.group(1)}.{two.group(2)}".lower() in ABBREVIATIONS)


def _sentences(text: str) -> list[str]:
    """断句：中文句号问号叹号、换行直接断；英文标点后要有空白才断，句号前是常见缩写（z.B. / z. B. / ca. / bzw. / e.g.）、
    或像“1.6 TDI”这样夹在数字中间的不断。"""
    out = []
    for part in re.split(r"[\n。！？]+", text or ""):
        start = 0
        for m in _SENT_END.finditer(part):
            if part[m.start()] == ".":
                word = re.search(r"([^\s(（\[]+)$", part[start:m.start()])
                token = (word.group(1) if word else "").lower().rstrip(".")
                if token in ABBREVIATIONS or token[-1:].isdigit() and part[m.end():m.end() + 1].isdigit():
                    continue
                if _spaced_abbreviation(part, start, m):
                    continue
            out.append(part[start:m.start() + 1])
            start = m.end()
        out.append(part[start:])
    return [s for s in out if s.strip()]
MAX_TOKENS = 400
MAX_ISSUES, MAX_RESOLVED, MAX_PROMISES, MAX_SKUS = 6, 4, 4, 5
SUMMARY_CHARS = 40

# 短追问、催问（多语言）：模型没给出沿用问题时，程序据此把前面未解决的问题接上
FOLLOW_UP = re.compile(
    r"\b(any (news|update|progress)|update\??$|still (not|nothing|waiting|no)|what about|how about|status\??$|"
    r"any (answer|reply)|is there (any )?news)\b|"
    r"gibt es (schon )?(neuigkeiten|etwas neues|was neues)|schon was (neues|gehört)|(immer|noch) (noch )?nicht|"
    r"wie sieht es aus|was ist (damit|mit meinem)|"
    r"des nouvelles|toujours pas|toujours rien|novità|ancora niente|ancora nulla|novedades|todavía no|sigue sin|"
    r"有进展|有消息|怎么样了|还是不行|还没|进度", re.I)


def _short(text: str | None, n: int = SUMMARY_CHARS) -> str:
    t = " ".join(str(text or "").split())
    return t[:n]


def _day(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d") if dt else None


# ------------------------------------------------------------------ 系统事实（权威）
def system_issues(db: Session, conv: Conversation, observations: list[dict] | None = None) -> list[dict]:
    """按库里的事实列出这个会话还没结束的问题。observations：本次预取的工具结果（补上 eBay 售后状态）。"""
    out: list[dict] = []
    for t in db.scalars(select(LogisticsTicket).where(LogisticsTicket.conversation_id == conv.id,
                                                      LogisticsTicket.handled_at.is_(None))
                        .order_by(LogisticsTicket.id)):
        if t.io_status == 8:  # 谷仓已完结
            continue
        draft = (t.io_code or "").startswith("DRAFT-")
        flags = {LOGISTICS} | ({NOT_RECEIVED} if "妥投未收到" in (t.type_name or "") else set())
        status = "查件单草稿待提交" if draft else ("谷仓已回复" if t.replied_at else "已提交谷仓，等承运商回复")
        out.append({"id": f"sys:ticket:{t.io_code}", "source": "system",
                    "intent": "delivery_delay", "summary_zh": _short(f"谷仓查件（{t.type_name or '查件'}）"),
                    "order_id": t.reference_no, "since": _day(t.submitted_at or t.created_at or t.synced_at),
                    "status": status, "refs": [] if draft else [t.io_code], "flags": sorted(flags)})
    for p in db.scalars(select(ParcelFollowup).where(ParcelFollowup.conversation_id == conv.id, ParcelFollowup.status == "OPEN")):
        flags = {LOGISTICS} | ({NOT_RECEIVED} if p.reason in ("妥投未收到", "未收到货纠纷") else set())
        out.append({"id": f"sys:parcel:{p.id}", "source": "system", "intent": "delivery_delay",
                    "summary_zh": _short(f"异常包裹跟进：{p.reason}{('，' + p.reason_note) if p.reason_note else ''}"),
                    "order_id": p.order_id, "since": p.occurred_on or _day(p.created_at),
                    "status": _short(p.next_step or "跟进中", 30), "refs": [x for x in (p.ticket_no or "").split(",") if x][:3],
                    "flags": sorted(flags)})
    seen_cases: set[str] = set()
    kind_info = {"INQUIRY": ("delivery_delay", "eBay 未收到货咨询", {LOGISTICS, NOT_RECEIVED}),
                 "CASE": ("return_refund", "eBay 纠纷", set()), "RETURN": ("return_refund", "eBay 退货", set()),
                 "PAYMENT_DISPUTE": ("return_refund", "付款纠纷", set()), "CANCEL": ("cancel_request", "取消交易", set())}
    if conv.buyer_key:
        for c in db.scalars(select(EbayCase).where(EbayCase.store_id == conv.store_id, EbayCase.is_open.is_(True),
                                                   func.lower(EbayCase.buyer) == conv.buyer_key.lower(),
                                                   EbayCase.kind.in_(tuple(kind_info)))):
            intent, label, flags = kind_info[c.kind]
            seen_cases.add(c.external_id)
            out.append({"id": f"sys:case:{c.kind}:{c.external_id}", "source": "system", "intent": intent,
                        "summary_zh": _short(f"{label}{('：' + c.reason) if c.reason else ''}"), "order_id": c.order_id,
                        "since": _day(c.opened_at), "status": _short(c.status or "未结", 30), "refs": [c.external_id],
                        "flags": sorted(flags)})
    for o in observations or []:  # 预取到的售后状态（eBay 实时 / 演示数据）
        if o.get("tool_name") != "get_case_status" or o.get("status") != "FOUND":
            continue
        for c in o.get("data") or []:
            if not isinstance(c, dict) or str(c.get("status") or "").upper() not in ("OPEN", "ESCALATED", "WAITING_SELLER") \
                    or str(c.get("case_id")) in seen_cases:
                continue
            kind = CASE_ALIASES.get(str(c.get("case_type") or "").upper(), str(c.get("case_type") or "").upper())
            intent, label, flags = kind_info.get(kind, ("return_refund", f"eBay {kind or '售后'}", set()))
            seen_cases.add(str(c.get("case_id")))
            out.append({"id": f"sys:case:{kind}:{c.get('case_id')}", "source": "system", "intent": intent, "observed": True,
                        "summary_zh": _short(f"{label}：{c.get('last_action') or ''}"), "order_id": c.get("order_id"),
                        "since": str(c.get("opened_at") or "")[:10] or None, "status": "未结", "refs": [str(c.get("case_id"))],
                        "flags": sorted(flags)})
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv.id,
                                                 InvoiceRequest.status.in_(("WAITING_EMAIL", "OPEN"))))
    if req is not None:
        out.append({"id": "sys:invoice", "source": "system", "intent": "invoice_vat",
                    "summary_zh": "买家要发票" + ("（增值税发票）" if req.invoice_type == "VAT" else ""), "order_id": req.order_id,
                    "since": _day(req.created_at), "status": "等买家给邮箱" if req.status == "WAITING_EMAIL" else "待审核发送",
                    "refs": [], "flags": [INVOICE]})
    cat_intent = {"质量问题": "quality_issue", "破损错发缺件": "damaged_wrong", "安装技术问题": "install_help", "适配核对": "fitment"}
    for c in db.scalars(select(AftersalesCase).where(AftersalesCase.conversation_id == conv.id,
                                                     AftersalesCase.status.in_(("SENT", "REPLIED")))):
        out.append({"id": f"sys:feishu:{c.id}", "source": "system", "intent": cat_intent.get(c.category, "quality_issue"),
                    "summary_zh": _short(f"售后群工单（{c.category}）：{c.problem_zh}"), "order_id": c.order_id,
                    "since": _day(c.sent_at or c.created_at),
                    "status": "群里已回复，待回复买家" if c.status == "REPLIED" else "已发售后群，等技术回复", "refs": [],
                    "flags": []})
    _attach_order_keys(db, conv.store_id, out)
    return out


def norm_order(order_id: str | None) -> str | None:
    """订单号比对前统一：去掉首尾空格、转大写。"""
    v = (order_id or "").strip().upper()
    return v or None


def is_legacy_order(order_id: str | None) -> bool:
    """旧格式“商品号-交易号”（eBay 咨询 / 纠纷里的订单号），和新格式订单号（如 12-34567-89012）不能直接比。"""
    return bool(order_id and LEGACY_ORDER.match(order_id.strip()))


def _attach_order_keys(db: Session, store_id: str, issues: list[dict]) -> None:
    """系统问题的订单号换算成新格式（order_key）：旧格式“商品号-交易号”按订单商品明细（ebay_order_lines 的
    line_key / legacy_order_id）找到新格式订单号；找不到就不写 order_key（比对时当作没写订单）。"""
    from .models import EbayOrderLine

    legacy = {i["order_id"].strip() for i in issues if is_legacy_order(i.get("order_id"))}
    found: dict[str, str] = {}
    if legacy:
        for key, old, oid in db.execute(select(EbayOrderLine.line_key, EbayOrderLine.legacy_order_id, EbayOrderLine.order_id)
                                        .where(EbayOrderLine.store_id == store_id,
                                               (EbayOrderLine.line_key.in_(legacy)) | (EbayOrderLine.legacy_order_id.in_(legacy)))):
            for k in (key, old):
                if k in legacy:
                    found[k] = oid
    for i in issues:
        oid = (i.get("order_id") or "").strip()
        if not oid:
            continue
        if is_legacy_order(oid):
            if oid in found:
                i["order_key"] = norm_order(found[oid])
        else:
            i["order_key"] = norm_order(oid)


def system_facts(db: Session, conv: Conversation, issues: list[dict]) -> dict:
    """程序补的事实：系统问题里的订单号、买家发过的图片 / 视频数量。"""
    images = videos = 0
    for mt, n in db.execute(select(MessageAttachment.media_type, func.count(MessageAttachment.id))
                            .join(Message, Message.id == MessageAttachment.message_id)
                            .where(Message.conversation_id == conv.id, Message.sender_role == "BUYER")
                            .group_by(MessageAttachment.media_type)):
        if str(mt or "").startswith("video"):
            videos += n
        else:
            images += n
    media = "、".join(x for x in (f"图片 {images} 张" if images else "", f"视频 {videos} 个" if videos else "") if x)
    return {"order_ids": [i["order_id"] for i in issues if i.get("order_id")], "media": f"已收到买家{media}" if media else None}


# ------------------------------------------------------------------ 组装、合并、裁剪
def _empty() -> dict:
    return {"topic": "", "issues": [], "resolved": [], "facts": {}, "promises": []}


def _same_problem(model_issue: dict, sys_issues: list[dict]) -> bool:
    """模型列的问题和系统问题是同一件事（同一订单或系统那条没写订单，且标记或意图相同）：以系统为准。"""
    flags = set(model_issue.get("flags") or []) | INTENT_FLAGS.get(model_issue.get("intent"), set())
    for s in sys_issues:
        same_order = not s.get("order_id") or not model_issue.get("order_id") or s["order_id"] == model_issue["order_id"]
        if same_order and (s.get("intent") == model_issue.get("intent") or flags & (set(s.get("flags") or []) - {LOGISTICS})
                           or (LOGISTICS in flags and LOGISTICS in (s.get("flags") or []))):
            return True
    return False


def _merge_facts(old: dict, new: dict | None, sys: dict) -> dict:
    f = dict(old or {})
    for k in ("vin", "vehicle"):
        v = _short((new or {}).get(k), 60)
        if v:
            f[k] = v
    for k, cap in (("skus", MAX_SKUS), ("order_ids", MAX_SKUS)):
        vals = [str(x) for x in [*(f.get(k) or []), *((new or {}).get(k) or []), *(sys.get(k) or [] if k == "order_ids" else [])] if x]
        f[k] = list(dict.fromkeys(vals))[-cap:]
    if sys.get("media"):
        f["media"] = sys["media"]
    return {k: v for k, v in f.items() if v}


def _trim(card: dict) -> dict:
    """控制在 MAX_TOKENS 内，按顺序砍：已解决 → 承诺（留 1 条）→ 模型问题 → 系统问题（极端情况下，
    系统问题多到还超长时从后往前砍，至少留 1 条）。正常会话远到不了上限：系统部分一般 40–90 token。"""
    card["issues"] = card["issues"][:MAX_ISSUES]
    card["resolved"] = card["resolved"][-MAX_RESOLVED:]
    card["promises"] = card["promises"][:MAX_PROMISES]
    while tokens(card) > MAX_TOKENS:
        if card["resolved"]:
            card["resolved"].pop(0)
        elif len(card["promises"]) > 1:
            card["promises"].pop()
        elif any(i["source"] == "model" for i in card["issues"]):
            idx = max(i for i, x in enumerate(card["issues"]) if x["source"] == "model")
            card["issues"].pop(idx)
        elif len(card["issues"]) > 1:
            card["issues"].pop()
        else:
            break
    return card


def tokens(card: dict) -> int:
    return estimate_tokens(render(card))


def current(db: Session, conv: Conversation, observations: list[dict] | None = None) -> dict:
    """起草前用的卡片：存着的模型部分 + 现在库里的系统事实。"""
    stored = conv.state_card or _empty()
    sys = system_issues(db, conv, observations)
    if observations is None:
        # 预取之前：上一轮实时工具查到、还没同步进 ebay_cases 的售后（如 INR 未收到货咨询）先沿用，
        # 让“要不要查包裹”能看到它；预取后用这一轮的实时结果重建（结了就消失）
        ids = {i["id"] for i in sys}
        sys += [i for i in stored.get("issues") or [] if i.get("source") == "system" and i.get("observed") and i["id"] not in ids]
    model = [i for i in stored.get("issues") or [] if i.get("source") == "model" and not _same_problem(i, sys)]
    card = {"topic": stored.get("topic") or "", "issues": [*sys, *model], "resolved": list(stored.get("resolved") or []),
            "facts": _merge_facts(stored.get("facts") or {}, None, system_facts(db, conv, sys)),
            "promises": list(stored.get("promises") or [])}
    return _trim(card)


def merge(before: dict, update: dict | None, sys_issues: list[dict], sys_facts: dict, *, inherits: str | None = None,
          intent: str | None = None, order_id: str | None = None, rule_flags: set[str] | None = None) -> dict:
    """落库时合并：before 起草前的卡片；update 模型的 state_update（没有起草为 None）；系统问题、事实以库里为准；
    rule_flags：本轮关键词规则认出的标记（没收到、补发、空包裹），挂到沿用的问题或本次主意图的问题上。"""
    today = utcnow().strftime("%Y-%m-%d")
    prev_model = {i["id"]: i for i in before.get("issues") or [] if i.get("source") == "model"}
    resolved = list(before.get("resolved") or [])
    model: list[dict] = []
    if update is None:
        model = list(prev_model.values())
    else:
        for u in update.get("open_issues") or []:
            code = u.get("intent") or "other"
            oid = _short(u.get("order_id"), 40) or None
            iid = f"m:{code}:{oid or ''}"
            if any(x["id"] == iid for x in model):
                continue
            prev = prev_model.get(iid) or {}
            model.append({"id": iid, "source": "model", "intent": code,
                          "summary_zh": _short(u.get("summary_zh") or prev.get("summary_zh")), "order_id": oid,
                          "since": prev.get("since") or today, "status": _short(u.get("status_zh") or prev.get("status"), 30),
                          "refs": prev.get("refs") or [],
                          "flags": sorted(set(prev.get("flags") or []) | INTENT_FLAGS.get(code, set()))})
        kept = {x["id"] for x in model}
        for iid, prev in prev_model.items():  # 模型这次没再列出：当作已解决
            if iid not in kept and iid != inherits:
                resolved.append({"summary_zh": prev.get("summary_zh"), "intent": prev.get("intent"), "at": today})
            elif iid not in kept:
                model.append(prev)  # 本次明说沿用的问题不能被漏掉
        for text in update.get("resolved") or []:
            if _short(text):
                resolved.append({"summary_zh": _short(text), "intent": None, "at": today})
    if rule_flags:
        # 挂到：沿用的问题 → 同意图的问题 → 已有的物流问题（规则标记都是物流类）→ 都没有才新建
        target = next((x for x in [*sys_issues, *model] if x["id"] == inherits), None) or \
            next((x for x in model if x.get("intent") == intent), None) or \
            next((x for x in [*sys_issues, *model] if LOGISTICS in (x.get("flags") or [])
                  and (not order_id or not x.get("order_id") or x["order_id"] == order_id)), None)
        if target is None:
            target = {"id": f"m:{intent or 'delivery_delay'}:{order_id or ''}", "source": "model",
                      "intent": intent or "delivery_delay", "summary_zh": _short("；".join(FLAG_LABELS[f] for f in sorted(rule_flags))),
                      "order_id": order_id, "since": today, "status": "待处理", "refs": [], "flags": []}
            model.append(target)
        target["flags"] = sorted(set(target.get("flags") or []) | rule_flags | {LOGISTICS})
    model = [i for i in model if not _same_problem(i, sys_issues)]
    promises = before.get("promises") or []
    if update is not None and update.get("promises") is not None:
        promises = [_short(p, 40) for p in update.get("promises") or [] if _short(p)]
    card = {"topic": _short((update or {}).get("topic"), 30) or before.get("topic") or "",
            "issues": [*sys_issues, *model], "resolved": resolved,
            "facts": _merge_facts(before.get("facts") or {}, (update or {}).get("facts"), sys_facts),
            "promises": list(promises)}
    return _trim(card)


def save(db: Session, conv: Conversation, card: dict) -> None:
    conv.state_card, conv.state_updated_at = card, utcnow()


# ------------------------------------------------------------------ 给提示词 / 规则用
def render(card: dict) -> str:
    """提示词里的状态卡（紧凑文本）。"""
    lines = [f"主题：{card.get('topic') or '（还没有）'}", "未解决问题（id｜来源｜意图｜说明｜订单｜开始｜状态）："]
    for i in card.get("issues") or []:
        lines.append("- " + "｜".join([i["id"], "系统" if i["source"] == "system" else "对话", i.get("intent") or "",
                                       i.get("summary_zh") or "", i.get("order_id") or "-", i.get("since") or "-",
                                       i.get("status") or "-"]))
    if not card.get("issues"):
        lines.append("- 无")
    if card.get("resolved"):
        lines.append("已解决：" + "；".join(r.get("summary_zh") or "" for r in card["resolved"]))
    facts = card.get("facts") or {}
    if facts:
        lines.append("关键事实：" + json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    if card.get("promises"):
        lines.append("我们答应过：" + "；".join(card["promises"]))
    return "\n".join(lines)


def issue(card: dict, issue_id: str | None) -> dict | None:
    return next((i for i in card.get("issues") or [] if i["id"] == issue_id), None) if issue_id else None


def open_flags(card: dict | None) -> set[str]:
    return {f for i in (card or {}).get("issues") or [] for f in i.get("flags") or []}


def is_follow_up(text: str | None) -> bool:
    """来信里有一句短的追问、催问（“有进展吗”“any news”“immer noch nicht”）。按句子看：长消息里追问一句、
    再问别的也算；追问词埋在长句子里（如“我还没决定要不要……”）不算。"""
    for s in _sentences(text or ""):
        s = s.strip()
        if s and len(s) <= FOLLOW_UP_SENTENCE and FOLLOW_UP.search(s):
            return True
    return False


def _order_ok(issue_: dict, orders: list[str] | None) -> bool:
    """问题的订单和本轮订单对得上：本轮订单唯一时只接同订单或没写订单的问题；不明确时只接没写订单的；
    orders 为 None（调用方不看订单）不过滤。"""
    if orders is None:
        return True
    # 两边换成同一种订单号再比（首尾空格、大小写不同也算同一单）；旧格式“商品号-交易号”换算不了的当作没写订单
    key = issue_.get("order_key") or (None if is_legacy_order(issue_.get("order_id")) else norm_order(issue_.get("order_id")))
    if not key:
        return True
    wanted = {norm_order(o) for o in orders if norm_order(o)}
    return len(wanted) == 1 and key in wanted


def carried_flags(card: dict | None, inherits: str | None, buyer_text: str, intents: list[str] | None = None,
                  orders: list[str] | None = None) -> set[str]:
    """本轮应该接上的标记。
    1. 模型说沿用了哪个问题：用那个问题的标记；
    2. 模型没给出沿用问题，而来信是短追问、催问（“有进展吗”“还是不行”）：
       - 没有模型输出（没起草），或模型自己也归不了类（意图只有 other）：接上全部未解决问题的标记；
       - 模型判了明确的意图：只接和这些意图有关的问题——同意图的问题，以及意图对应的标记
         （物流意图接没收到 / 补发 / 空包裹，发票意图接发票）。判成适配、商品咨询等新话题时，句子里碰巧有
         “what about / noch nicht / 还没”也不会被当成追问没收到；退货退款意图只接 eBay 售后单上的标记；
       - 只接订单对得上的问题（orders：本轮的候选订单，见 _order_ok），另一笔订单的“没收到”不会接到这笔上；
    3. 其余（明显的新话题）不接。追问按句子判断（is_follow_up）。"""
    hit = issue(card or {}, inherits)
    if hit is not None:
        return set(hit.get("flags") or [])
    if not is_follow_up(buyer_text):
        return set()
    issues = [x for x in (card or {}).get("issues") or [] if _order_ok(x, orders)]
    clear = [i for i in intents or [] if i != "other"]
    if not clear:
        return {f for x in issues for f in x.get("flags") or []}
    wanted = {f for i in clear for f in INTENT_RELATED_FLAGS.get(i, set())}
    case_wanted = {f for i in clear for f in CASE_INTENTS.get(i, set())}
    out: set[str] = set()
    for x in issues:
        flags = set(x.get("flags") or [])
        if x.get("intent") in clear:
            out |= flags
        elif flags & wanted:
            out |= flags & wanted
        elif case_wanted and str(x.get("id", "")).startswith("sys:case:"):
            out |= flags & case_wanted
    return out
