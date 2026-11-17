"""从本地库抽黄金回归集（只读）：

来源（按可信度排）：
  agent     AI 草稿经人工审核后发出的任务：有当时的模型输出、工具结果、知识引用、人工最终发出的回复和修改比例
  manual    工作台会话里人工回复过的买家消息（店铺发出的消息 + 它之前的买家消息；有当时的回复任务就带上工具结果）
  baseline  上线前补拉的历史会话里“买家消息 → 店铺回复”的配对（只有对话，没有工具结果）
  synthetic tests/eval/intent_cases.json 的合成多轮用例（有人工标注的期望意图 / action，没有回复，只在 --live 时评）
按“主意图 × 语言”分层，每层最多 per_stratum 条，总数最多 limit 条；同一来源原始 ID 的 case_id 稳定，重抽不变。
脱敏：每条用一个 PiiMasker——已知值（订单 / 收件信息里的姓名、地址、电话、邮箱，买家 eBay 用户名）+ 规则兜底；
店铺换成别名 S1、S2…；买家用户名、会话 ID、消息 ID 都不写进去。
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..guards.pii import PiiMasker, known_pii
from . import autolabel, review
from .golden import ROOT, case_id

HISTORY_MAX = 8      # 每条最多带几条历史消息
TOOL_KEYS = ("tool_name", "arguments", "status", "data", "evidence_ref", "message")


def _ago(t: datetime | None, ref: datetime | None) -> str:
    if not t or not ref:
        return "0m"
    mins = max(0, int((ref - t).total_seconds() // 60))
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}d" if d else "") + (f"{h}h" if h else "") + (f"{m}m" if m or not (d or h) else "")


class _Aliases:
    def __init__(self):
        self.stores: dict[str, str] = {}

    def store(self, sid: str) -> str:
        if sid not in self.stores:
            self.stores[sid] = f"S{len(self.stores) + 1}"
        return self.stores[sid]


ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿]")


def _clean(t: str | None) -> str:
    """去掉零宽字符（eBay / ERP 发出的消息里常带一串不可见字符）和首尾空白。"""
    return ZERO_WIDTH.sub("", t or "").strip()


def _finish(case: dict, masker: PiiMasker) -> dict:
    """清理、脱敏、补自动期望。"""
    inp = case["input"]
    inp["title"] = masker.mask(_clean(inp.get("title")))
    inp["history"] = [{**m, "text": masker.mask(_clean(m["text"]))} for m in inp["history"] if _clean(m["text"])]
    inp["inbound"] = [{**m, "text": masker.mask(_clean(m["text"]))} for m in inp["inbound"] if _clean(m["text"])]
    inp["tools"] = masker.mask_obj(inp.get("tools") or [])
    inp["knowledge"] = inp.get("knowledge") or []
    if inp.get("model_output"):
        mo = masker.mask_obj(inp["model_output"])
        inp["model_output"] = mo
    case["human_reply"] = masker.mask(_clean(case.get("human_reply")))
    buyer = "\n".join(m["text"] for m in inp["inbound"])
    if not case.get("expect"):
        evidence = buyer + "\n" + json.dumps(inp["tools"], ensure_ascii=False)
        case["expect"] = autolabel.expect_for(buyer, case["human_reply"], evidence, inp.get("model_output"))
        case["label_source"], case["needs_review"] = "auto", True
    case["language"] = case.get("language") or autolabel.language(buyer) or "en"
    case["stratum"] = case["expect"]["intent"][0] if case["expect"].get("intent") else "other"
    return case


def _model_output(d) -> dict:
    """草稿行 → 当时模型的结构化输出（回放时假模型原样返回）。"""
    return {"intent": d.intent or "other", "intents": d.intents or [d.intent or "other"], "intent_confidence": d.intent_confidence,
            "intent_basis": [], "inherits_issue": None, "language": d.language or "en", "action": d.action or "answer",
            "needs_human": d.action == "handoff", "claims": d.claims or [], "missing_facts": d.missing_facts or [],
            "conflicts": d.conflicts or [], "reasoning_zh": d.reasoning_zh or "", "reply_text": d.text or "",
            "reply_zh": d.text_zh or "", "situation_stage": "other", "situation_zh": "", "reason_code": "golden_replay"}


def _model_meta(db: Session, job_id: int, d) -> dict:
    """当时起草用的版本：以后分数变了能分清是规则改了、提示词 / 模型换了，还是知识变了。"""
    from ..models import AgentRun, AgentStep

    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id, AgentRun.run_generation == d.run_generation))
    step = db.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node == "compose_reply")) if run else None
    return {"prompt_version": d.prompt_version or (run.prompt_version if run else None),
            "model": ((step.detail or {}).get("model") if step else None),
            "model_config_version": d.model_config_version or (run.model_config_version if run else None),
            "rule_version": run.rule_version if run else None, "knowledge_versions": d.knowledge_versions or [],
            "compliance_rules_version": d.compliance_rules_version, "drafted_at": d.created_at.isoformat() if d.created_at else None}


def _tools(db: Session, job_id: int, generation: int) -> list[dict]:
    from ..models import ToolCall

    return [{"tool_name": t.tool_name, "arguments": t.arguments_redacted or {}, "status": t.result_status,
             "data": (t.result_data or {}).get("data"), "evidence_ref": t.evidence_ref,
             "message": (t.result_data or {}).get("message")}
            for t in db.scalars(select(ToolCall).where(ToolCall.job_id == job_id, ToolCall.run_generation == generation)
                                .order_by(ToolCall.id))]


def _knowledge(db: Session, store_id: str, evidence_ids: list[str] | None) -> list[dict]:
    from ..models import KnowledgeChunk

    refs = {tuple(e[3:].split("@", 1)) for e in evidence_ids or [] if e.startswith("kb:") and "@" in e}
    if not refs:
        return []
    rows = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.clause_id.in_({c for c, _ in refs}),
                                                   KnowledgeChunk.store_id.in_((store_id, "ALL")))).all()
    return [{"clause_id": r.clause_id, "version": r.version, "title": r.title, "text": r.text}
            for r in rows if (r.clause_id, r.version) in refs]


def _split(msgs: list, upto_seq: int) -> tuple[list, list]:
    """一条店铺回复（seq=upto_seq）之前：最后一段连续的买家消息是本次来信，再往前是历史。"""
    before = [m for m in msgs if m.conversation_seq < upto_seq and m.sender_role in ("BUYER", "SELLER")
              and m.auto_kind != "ACK"]
    inbound: list = []
    while before and before[-1].sender_role == "BUYER":
        inbound.insert(0, before.pop())
    return before[-HISTORY_MAX:], inbound


def from_agent(db: Session, aliases: _Aliases) -> list[dict]:
    from ..models import Conversation, Draft, JobState, Message, OutboundMessage, ReplyJob, ReviewFeedback

    out = []
    for d, job in db.execute(select(Draft, ReplyJob).join(ReplyJob, ReplyJob.id == Draft.job_id)
                             .where(Draft.status == "APPROVED", ReplyJob.state == JobState.SENT)):
        ob = db.scalar(select(OutboundMessage).where(OutboundMessage.job_id == job.id, OutboundMessage.state == "SENT")
                       .order_by(OutboundMessage.id.desc()).limit(1))
        if ob is None or ob.message_id is None:
            continue
        conv = db.get(Conversation, job.conversation_id)
        sent = db.get(Message, ob.message_id)
        msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq)).all()
        hist, inbound = _split(msgs, sent.conversation_seq)
        if not inbound:
            continue
        agent_draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.author == "agent")
                                .order_by(Draft.id.desc()).limit(1)) or d
        fb = db.scalar(select(ReviewFeedback).where(ReviewFeedback.job_id == job.id).order_by(ReviewFeedback.id.desc()).limit(1))
        tools = _tools(db, job.id, agent_draft.run_generation)
        masker = PiiMasker(known={**known_pii([t["data"] for t in tools]), "NAME": [conv.buyer_key,
                                                                                  *known_pii([t["data"] for t in tools])["NAME"]]})
        ref = inbound[-1].sent_at
        out.append(_finish({
            "case_id": case_id("agent", f"{conv.store_id}:{job.id}"), "source": "agent", "store": aliases.store(conv.store_id),
            "marketplace": conv.marketplace_id,
            "input": {"title": conv.conversation_title or "",
                      "history": [{"role": m.sender_role, "text": m.body or "", "ago": _ago(m.sent_at, ref)} for m in hist],
                      "inbound": [{"text": m.body or "", "ago": _ago(m.sent_at, ref)} for m in inbound],
                      "tools": tools, "knowledge": _knowledge(db, conv.store_id, agent_draft.evidence_ids),
                      "model_output": _model_output(agent_draft) if agent_draft.author == "agent" else None,
                      "model_meta": _model_meta(db, job.id, agent_draft)},
            "human_reply": ob.body,
            "_job_id": job.id,
            "meta": {"edit_ratio": fb.edit_ratio if fb else None, "review_action": fb.action if fb else None,
                     "intent_confidence": agent_draft.intent_confidence, "risk_level": agent_draft.risk_level,
                     "review_reasons": agent_draft.review_reasons or []},
        }, masker))
    return out


def from_manual(db: Session, aliases: _Aliases, skip_jobs: set[int]) -> list[dict]:
    from ..models import Conversation, Message, ReplyJob, ReplyJobSource

    out = []
    for conv in db.scalars(select(Conversation).where(Conversation.conversation_type != "FROM_EBAY")):
        msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.conversation_seq)).all()
        for m in msgs:
            if m.sender_role != "SELLER" or m.direction != "OUT" or m.auto_kind == "ACK" or not (m.body or "").strip():
                continue
            hist, inbound = _split(msgs, m.conversation_seq)
            if not inbound:
                continue
            job_id = db.scalar(select(ReplyJobSource.job_id).where(ReplyJobSource.store_id == conv.store_id,
                                                                   ReplyJobSource.source_message_id == inbound[-1].ebay_message_id))
            if job_id in skip_jobs:
                continue  # 已经作为 agent 来源抽过
            job = db.get(ReplyJob, job_id) if job_id else None
            tools = _tools(db, job.id, job.run_generation) if job else []
            known = known_pii([t["data"] for t in tools])
            masker = PiiMasker(known={**known, "NAME": [conv.buyer_key, *known["NAME"]]})
            ref = inbound[-1].sent_at
            out.append(_finish({
                "case_id": case_id("manual", f"{conv.store_id}:{m.ebay_message_id}"), "source": "manual",
                "store": aliases.store(conv.store_id), "marketplace": conv.marketplace_id,
                "input": {"title": conv.conversation_title or "",
                          "history": [{"role": x.sender_role, "text": x.body or "", "ago": _ago(x.sent_at, ref)} for x in hist],
                          "inbound": [{"text": x.body or "", "ago": _ago(x.sent_at, ref)} for x in inbound],
                          "tools": tools, "knowledge": [], "model_output": None},
                "human_reply": m.body, "meta": {},
            }, masker))
    return out


def from_baseline(db: Session, aliases: _Aliases) -> list[dict]:
    from ..models import BaselineConversation, BaselineMessage

    out = []
    for conv in db.scalars(select(BaselineConversation)):
        msgs = db.scalars(select(BaselineMessage).where(BaselineMessage.conversation_id == conv.id)
                          .order_by(BaselineMessage.sent_at, BaselineMessage.id)).all()
        for i, m in enumerate(msgs):
            if m.sender_role != "SELLER" or not (m.body or "").strip():
                continue
            before = msgs[:i]
            inbound = []
            while before and before[-1].sender_role == "BUYER":
                inbound.insert(0, before.pop())
            if not inbound or not any((x.body or "").strip() for x in inbound):
                continue
            hist = before[-HISTORY_MAX:]
            masker = PiiMasker(known={"NAME": [conv.buyer_key] if conv.buyer_key else []})
            ref = inbound[-1].sent_at
            out.append(_finish({
                "case_id": case_id("baseline", f"{conv.store_id}:{m.ebay_message_id}"), "source": "baseline",
                "store": aliases.store(conv.store_id), "marketplace": None,
                "input": {"title": "", "history": [{"role": x.sender_role, "text": x.body or "", "ago": _ago(x.sent_at, ref)}
                                                   for x in hist],
                          "inbound": [{"text": x.body or "", "ago": _ago(x.sent_at, ref)} for x in inbound if (x.body or "").strip()],
                          "tools": [], "knowledge": [], "model_output": None},
                "human_reply": m.body, "meta": {},
            }, masker))
    return out


def from_synthetic() -> list[dict]:
    """合成多轮意图用例（已人工标注期望）：没有回复，只在 --live 时评。"""
    data = json.loads((ROOT / "tests" / "eval" / "intent_cases.json").read_text(encoding="utf-8"))
    action_map = {"reply": "answer", "clarify": "clarify", "handoff": "handoff"}
    out = []
    for c in data["cases"]:
        msgs = c["messages"]
        last_seller = max((i for i, m in enumerate(msgs) if m["role"] == "SELLER"), default=-1)
        hist, inbound = msgs[:last_seller + 1], msgs[last_seller + 1:]
        e = c.get("expect") or {}
        out.append({
            "case_id": case_id("synthetic", c["case_id"]), "source": "synthetic", "store": "SYN", "marketplace": c.get("marketplace"),
            "language": c.get("language"), "stratum": (e.get("intent") or ["other"])[0],
            "input": {"title": data["listings"].get(c.get("listing"), {}).get("title", ""),
                      "history": [{"role": m["role"], "text": m["text"], "ago": m.get("ago", "1d")} for m in hist],
                      "inbound": [{"text": m["text"], "ago": m.get("ago", "0m")} for m in inbound],
                      "tools": [], "knowledge": [], "model_output": None, "context": c.get("context") or {}},
            "human_reply": "",
            "expect": {"intent": e.get("intent") or [], "intent_not": e.get("intent_not") or [],
                       "action": action_map.get(e.get("action")), "handoff": (e.get("action") == "handoff") if e.get("action") else None,
                       "compliance": None, "must_include": [], "must_not_include": list(autolabel.GLOBAL_FORBIDDEN)},
            "label_source": "synthetic", "needs_review": False, "meta": {"tags": c.get("tags") or [], "origin": c["case_id"]},
        })
    return out


def stratify(cases: list[dict], limit: int = 300, per_stratum: int = 40, seed: int = 20261115) -> list[dict]:
    """按（主意图, 语言）分层抽样：每层最多 per_stratum 条，来源可信度高的优先；总数最多 limit 条。"""
    rank = {"agent": 0, "manual": 1, "baseline": 2, "synthetic": 3}
    rng = random.Random(seed)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in cases:
        groups[(c["stratum"], c.get("language") or "?")].append(c)
    picked: list[dict] = []
    for key in sorted(groups):
        g = groups[key]
        rng.shuffle(g)
        g.sort(key=lambda c: rank.get(c["source"], 9))
        picked += g[:per_stratum]
    if len(picked) > limit:  # 轮流从各层取，保证小层也有
        by = defaultdict(list)
        for c in picked:
            by[(c["stratum"], c.get("language"))].append(c)
        picked, keys = [], sorted(by)
        while len(picked) < limit and any(by.values()):
            for k in keys:
                if by[k] and len(picked) < limit:
                    picked.append(by[k].pop(0))
    return sorted(picked, key=lambda c: c["case_id"])


def build(db: Session, *, limit: int = 300, per_stratum: int = 40, synthetic: bool = True) -> tuple[list[dict], dict]:
    aliases = _Aliases()
    agent = from_agent(db, aliases)
    skip = {c.pop("_job_id") for c in agent}  # 已作为 agent 来源抽过的回复任务，manual 不再重复抽
    manual = from_manual(db, aliases, skip_jobs=skip)
    baseline = from_baseline(db, aliases)
    syn = from_synthetic() if synthetic else []
    allc = [c for c in [*agent, *manual, *baseline, *syn] if c["input"]["inbound"]]
    picked = stratify(allc, limit, per_stratum)
    stats = {"candidates": dict(Counter(c["source"] for c in allc)), "picked": dict(Counter(c["source"] for c in picked)),
             "strata": dict(Counter(c["stratum"] for c in picked)), "languages": dict(Counter(c.get("language") for c in picked)),
             "stores": len(aliases.stores), "needs_review": sum(1 for c in picked if c.get("needs_review")),
             "agent_cases": len(agent), "distribution": review.distribution(picked)}
    return picked, stats
