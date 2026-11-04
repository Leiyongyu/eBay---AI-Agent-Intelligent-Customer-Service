"""固定流程（fixed_pipeline）的 LangGraph 节点（第 14.1、14.3 节）。

load_snapshot → load_memory → observe_attachments → prefetch → load_knowledge → compose_reply → validate_output
    ↘（格式错误且未超修复上限）repair_output → validate_output
→ persist_candidate

图里没有发送节点：节点重放或崩溃重跑都不会外发消息。每个节点结束时提交一次，
把工具调用与步骤留痕先落库，也避免长时间的模型调用占住 SQLite 写锁。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.runtime import Runtime
from sqlalchemy import func, select

from ..db import utcnow
from ..guards import risk
from ..guards.claims import check_claims
from ..guards.compliance import ComplianceChecker
from ..guards.language import language_mismatch, reply_language_hint
from ..intents import intent_catalog_text
from ..invoicing.service import detect_invoice_email, ensure_waiting, record_request
from ..knowledge.retriever import estimate_tokens, load_full_context, render_clause, retrieve
from ..knowledge.vector_index import RagNotReady
from ..memory.service import build_recent
from ..media.service import attachments_for_messages, ensure_stored
from ..media.service import file_path as media_file_path
from ..models import (
    AgentStep,
    Aftersales,
    AttachmentStatus,
    AuditEvent,
    Conversation,
    Draft,
    ImageObservation,
    JobState,
    KnowledgeChunk,
    Message,
    MessageAttachment,
    ReplyJob,
    ReplyJobSource,
)
from ..prompts import load_prompt, prompt_version
from ..sending.service import AUTO_ACTOR, SendError, auto_reply_streak, simulate_send
from ..tools.executor import ToolBudgetExceeded, ToolExecutor
from ..tools.registry import MODEL_TOOL_DESCRIPTIONS, model_tool_specs
from ..logistics import followup as logistics_followup
from ..logistics.track17 import RISKY_STATUS as TRACK_RISKY_STATUS
from .. import auto_tags
from .. import usage as usage_log
from .schemas import ReplyDraftOutput
from .state import AgentDeps, AgentState

log = logging.getLogger(__name__)

PROMPT_NAMES = ("system_policy", "reply")

_ORDER_IN_TEXT = re.compile(r"\b\d{2}-\d{5}-[A-Z0-9]{2,5}\b")
# 买家在问物流（多语言）：包裹在哪、单号、发货、送达
_SHIPPING_HINT = re.compile(
    r"\b(where is|tracking|track|parcel|package|shipment|shipped|delivery|delivered|arrive\w*|courier)\b|"
    r"\b(wo (ist|bleibt)|sendung\w*|paket\w*|lieferung|zustellung|versand\w*|verschickt|geliefert|angekommen)\b|"
    r"\b(colis|livraison|suivi|pacco|spedizione|consegna|paquete|envío|entrega|seguimiento)\b|物流|快递|单号|发货|到货",
    re.I)


def step(fn):
    """记录 agent_steps（可读的步骤摘要，不保存模型思维过程），并在节点结束时提交。"""

    # 不用 functools.wraps：LangGraph 按签名里的 runtime 参数注入上下文，__wrapped__ 会让它看到原函数签名
    def wrapper(state: AgentState, runtime: Runtime[AgentDeps]):
        deps = runtime.context
        started = time.perf_counter()
        update = fn(state, deps) or {}
        detail = update.pop("_detail", None)
        deps.db.add(AgentStep(
            run_id=deps.run_id,
            node=fn.__name__,
            result_status=update.pop("_status", "OK"),
            detail=detail,
            usage=update.get("usage") if fn.__name__ in ("compose_reply", "repair_output") else None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))
        deps.db.commit()
        return update

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- 感知与记忆
@step
def load_snapshot(state: AgentState, deps: AgentDeps):
    db, run = deps.db, deps.run
    conv = db.get(Conversation, run.conversation_id)
    source_ids = db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == run.job_id)).all()
    buyer_msgs = db.scalars(
        select(Message).where(Message.store_id == run.store_id, Message.ebay_message_id.in_(source_ids))
        .order_by(Message.conversation_seq)
    ).all()
    snapshot = {
        "conversation_row_version": conv.row_version,
        "handoff_state": conv.handoff_state,
        "marketplace_id": conv.marketplace_id,
        "conversation_title": conv.conversation_title,
        "source_message_ids": [m.ebay_message_id for m in buyer_msgs],
        "source_db_ids": [m.id for m in buyer_msgs],
        "buyer_texts": [m.body for m in buyer_msgs],
        "snapshot_at": utcnow().isoformat(timespec="seconds") + "Z",
    }
    return {"snapshot": snapshot, "problems": [], "observations": [], "attachments": [], "repairs": 0, "usage": {},
            "_detail": {"sources": len(buyer_msgs), "row_version": conv.row_version}}


@step
def load_memory(state: AgentState, deps: AgentDeps):
    snap = state["snapshot"]
    memory = build_recent(deps.db, deps.run.store_id, deps.run.conversation_id, set(snap["source_message_ids"]))
    problems = list(state["problems"])
    if memory["missing_required"]:
        problems.append("CONTEXT_OVER_BUDGET")
    return {"memory": memory, "problems": problems,
            "_detail": {k: memory[k] for k in ("total_messages", "truncated", "tokens")}}


# ---------------------------------------------------------------- 附件（第 20 章）
def _observe(deps: AgentDeps, att: MessageAttachment, context: str) -> ImageObservation:
    """同一附件、同一视觉配置与提示词只识别一次；同一图片（哈希相同）复用已有结果，不重复计费。"""
    db, vision = deps.db, deps.vision
    versions = (ImageObservation.model_config_version == vision.config_version,
                ImageObservation.prompt_version == vision.prompt_version, ImageObservation.status == "OK")
    mine = db.scalar(select(ImageObservation).where(ImageObservation.attachment_id == att.id, *versions)
                     .order_by(ImageObservation.id.desc()).limit(1))
    if mine:
        return mine
    same = db.scalar(select(ImageObservation).where(ImageObservation.sha256 == att.sha256, *versions)
                     .order_by(ImageObservation.id.desc()).limit(1))
    obs = ImageObservation(attachment_id=att.id, sha256=att.sha256, model_config_version=vision.config_version,
                           prompt_version=vision.prompt_version, status="OK")
    if same:
        obs.observation, obs.cached_from_id = same.observation, same.id
    else:
        path = media_file_path(att)
        started = time.perf_counter()
        try:
            r = vision.observe(path.read_bytes(), att.content_type or "image/jpeg", context)
            obs.usage = r.usage or None
            if r.parsed is not None:
                obs.observation = r.parsed.model_dump()
            else:
                obs.status, obs.error = "FAILED", (r.error or "解析失败")[:500]
        except Exception as e:
            obs.status, obs.error = "FAILED", f"{type(e).__name__}: {str(e)[:300]}"
        usage_log.record(db, "vision", provider=deps.settings.vision_provider, model=deps.settings.vision_model,
                         usage=obs.usage, duration_ms=int((time.perf_counter() - started) * 1000),
                         ok=obs.status == "OK", error=obs.error, store_id=att.store_id,
                         conversation_id=att.conversation_id, job_id=deps.run.job_id)
    db.add(obs)
    db.flush()
    return obs


@step
def observe_attachments(state: AgentState, deps: AgentDeps):
    """本次任务买家消息的附件：eBay 图片按需下载并校验保存，再按配置用视觉模型做客观观察。

    观察结果只是线索（证据 ID img:附件ID），不能据此认定质量问题或同意退款补发；
    任何带附件的任务都进入人工审核，未被读取的附件会在审核原因里写明（风险闸门处理）。
    """
    db, s = deps.db, deps.settings
    snap = state["snapshot"]
    atts = attachments_for_messages(db, snap["source_db_ids"])
    if not atts:
        return {"attachments": [], "_status": "NONE"}
    ensure_stored(db, atts)
    ebay_ids = dict(zip(snap["source_db_ids"], snap["source_message_ids"]))
    conv = db.get(Conversation, deps.run.conversation_id)
    context = f"刊登 {deps.run.item_id or '无'}；{conv.conversation_title or ''}".strip("；")
    items, calls = [], 0
    for att in atts:
        item = {"attachment_id": att.id, "evidence_ref": f"img:{att.id}", "message_id": ebay_ids.get(att.message_id),
                "media_type": att.media_type, "status": "NOT_READ", "reason": None, "observation": None}
        if att.status != AttachmentStatus.STORED:
            item["reason"] = att.status_reason or att.status
        elif deps.vision is None:
            item["reason"] = "未开启视觉模型（VISION_ENABLED）"
        elif calls >= s.vision_max_images_per_job:
            item["reason"] = f"超过每个任务最多识别 {s.vision_max_images_per_job} 张的上限"
        elif deps.time_left() < 15:
            item["reason"] = "编排剩余时间不足，未识别"
        else:
            obs = _observe(deps, att, context)
            calls += obs.cached_from_id is None
            if obs.status == "OK":
                item.update(status="OBSERVED", observation=obs.observation, observation_id=obs.id)
            else:
                item["reason"] = f"识图失败：{obs.error}"
        items.append(item)
    db.flush()
    return {"attachments": items,
            "_detail": {"attachments": len(items), "observed": sum(i["status"] == "OBSERVED" for i in items),
                        "vision_calls": calls, "not_read": [i["reason"] for i in items if i["status"] != "OBSERVED"]}}


def _attachment_text(state: AgentState) -> str:
    """把图片里读到的文字（零件号、SKU 等）也当作预取线索，与买家文字同等对待。"""
    parts = []
    for a in state.get("attachments") or []:
        o = a.get("observation") or {}
        parts += o.get("visible_text", []) + o.get("part_numbers", [])
    return "\n".join(parts)


# ---------------------------------------------------------------- 确定性预取
@step
def prefetch(state: AgentState, deps: AgentDeps):
    """程序固定做的安全检查：买家订单 + 每单的售后状态（风险闸门据此判断未结 case，不能交给模型决定查不查）。

    商品详情、物流、SKU 与库存等由模型在生成回复时按需调用工具查询（原生工具调用，见 compose_reply）。
    全部经过 ToolExecutor 计入预算并留痕（第 12.1 节）。
    """
    ex = ToolExecutor(deps.db, deps.run, deps.source, deps.settings.agent_prefetch_max_tool_calls)
    text = "\n".join(state["snapshot"]["buyer_texts"]) + "\n" + _attachment_text(state)
    observations: list[dict] = []
    problems = list(state["problems"])

    def call(name, args=None):
        r = ex.execute(name, args)
        observations.append(r.model_dump())
        if r.status == "ERROR":
            problems.append("TOOL_ERROR")
        return r

    try:
        order_hint = _ORDER_IN_TEXT.search(text)
        orders = call("get_authorized_order_context", {"order_ref": order_hint.group(0)} if order_hint else {})
        if orders.status == "FOUND":
            call("get_case_status", {"order_id": orders.data["order_id"]})
            # 买家问物流：程序固定先查包裹（轨迹、断更天数、查件单），不依赖模型想不想查；转人工判断也靠这一步
            if _SHIPPING_HINT.search(text) or _NOT_RECEIVED.search(text) or _RESHIP.search(text):
                call("get_package_status", {"order_id": orders.data["order_id"]})
        elif orders.status == "AMBIGUOUS":
            # 多订单时不猜是哪一单，但每单都要看有没有未结 case，用于风险升级
            for o in orders.data[:3]:
                call("get_case_status", {"order_id": o["order_id"]})
    except ToolBudgetExceeded:
        problems.append("TOOL_BUDGET_EXCEEDED")

    return {"observations": observations, "problems": problems,
            "_detail": {"calls": [(o["tool_name"], o["status"]) for o in observations]}}


def _retrieval_query(state: AgentState) -> str:
    """检索用的问题：会话标题（多为刊登标题）+ 本次买家消息 + 图片里读到的文字。"""
    snap = state["snapshot"]
    parts = [snap.get("conversation_title") or "", *snap["buyer_texts"], _attachment_text(state)]
    return "\n".join(p for p in parts if p and p.strip())


@step
def load_knowledge(state: AgentState, deps: AgentDeps):
    s = deps.settings
    problems = list(state["problems"])
    on = s.knowledge_date()
    if s.knowledge_mode == "retrieval":
        try:
            with deps.db.begin_nested():  # 检索 SQL 出错时只回滚到这里，不影响本次运行的其他记录
                kc = retrieve(deps.db, deps.run.store_id, deps.run.marketplace_id, on, _retrieval_query(state),
                              top_k=s.knowledge_retrieval_top_k, candidates=s.knowledge_retrieval_candidates,
                              budget_tokens=s.knowledge_retrieval_max_tokens)
        except Exception as e:
            # 不悄悄退回全量注入：没有可靠知识就交人工（第 15.4 节）
            reasons = e.reasons if isinstance(e, RagNotReady) else [f"检索失败：{type(e).__name__}: {str(e)[:200]}"]
            problems.append("RAG_NOT_READY")
            return {"knowledge": {"clause_ids": [], "versions": [], "tokens": 0, "over_budget": False,
                                  "date": on.isoformat(), "mode": "retrieval"},
                    "problems": problems, "_status": "ERROR", "_detail": {"reasons": reasons}}
        detail = {"mode": "retrieval", "candidates": kc.candidates, "clauses": len(kc.clauses), "tokens": kc.tokens,
                  "query_tokens": kc.query_tokens[:40], "hits": kc.hits}
    else:
        kc = load_full_context(deps.db, deps.run.store_id, deps.run.marketplace_id, on,
                               s.knowledge_full_context_max_tokens)
        if kc.over_budget:
            # 超出全量注入预算：该店铺应切换检索模式，切换前交人工（第 15.5 节）
            problems.append("KNOWLEDGE_OVER_BUDGET")
        detail = {"mode": "full_context", "clauses": len(kc.clauses), "tokens": kc.tokens,
                  "budget": s.knowledge_full_context_max_tokens}
    knowledge = {
        "clause_ids": [c.chunk_id for c in kc.clauses],
        "versions": kc.versions,
        "tokens": kc.tokens,
        # 检索模式超预算只是少放几条（已按相关度截断），不阻断生成
        "over_budget": kc.over_budget if s.knowledge_mode != "retrieval" else False,
        "date": on.isoformat(),
        "mode": s.knowledge_mode,
    }
    return {"knowledge": knowledge, "problems": problems, "_detail": detail}


# ---------------------------------------------------------------- 生成
def _store_now(deps: AgentDeps) -> str:
    try:
        tz = ZoneInfo(deps.store.timezone)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    return f"{now:%Y-%m-%d %H:%M} {now:%A}（{deps.store.timezone}）"


def _history_attachment_counts(deps: AgentDeps, ebay_ids: list[str]) -> dict[str, int]:
    if not ebay_ids:
        return {}
    rows = deps.db.execute(
        select(Message.ebay_message_id, func.count(MessageAttachment.id))
        .join(MessageAttachment, MessageAttachment.message_id == Message.id)
        .where(Message.store_id == deps.run.store_id, Message.ebay_message_id.in_(ebay_ids))
        .group_by(Message.ebay_message_id)
    ).all()
    return {eid: n for eid, n in rows}


def _attachments_block(state: AgentState) -> list[str]:
    atts = state.get("attachments") or []
    if not atts:
        return []
    rows = [{"evidence_ref": a["evidence_ref"], "message_id": a["message_id"], "media_type": a["media_type"],
             "status": a["status"], "reason": a["reason"], "observation": a["observation"]} for a in atts]
    return [
        "<buyer_attachments>",
        "买家随本次消息发来的附件。status=OBSERVED 的 observation 是视觉模型的客观观察，未经人工确认，只能作为线索；"
        "status=NOT_READ 的附件你没有看过，不得声称已查看其内容。",
        json.dumps(rows, ensure_ascii=False),
        "</buyer_attachments>",
    ]


def build_messages(state: AgentState, deps: AgentDeps) -> list[BaseMessage]:
    run = deps.run
    clauses = deps.db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.chunk_id.in_(state["knowledge"]["clause_ids"]))
    ).all()
    order = {cid: i for i, cid in enumerate(state["knowledge"]["clause_ids"])}
    clauses = sorted(clauses, key=lambda c: order[c.chunk_id])

    system = "\n\n".join([
        load_prompt("system_policy"),
        load_prompt("reply").replace("{reply_language}",
                                     reply_language_hint((state["snapshot"]["buyer_texts"] or [""])[-1]))
        .replace("{intent_catalog}", intent_catalog_text()),
    ])

    history: list[BaseMessage] = []
    task_lines: list[str] = []
    task_atts: dict[str, list[str]] = {}
    for a in state.get("attachments") or []:
        task_atts.setdefault(a["message_id"], []).append(a["evidence_ref"])
    old_counts = _history_attachment_counts(deps, [m["id"] for m in state["memory"]["messages"] if not m["in_task"]])
    for m in state["memory"]["messages"]:
        if m["in_task"]:
            refs = task_atts.get(m["id"])
            note = f"（附件：{'、'.join(refs)}，见 <buyer_attachments>）" if refs else ""
            task_lines.append(f"[{m['id']} @ {m['at']}] {m['text'] or '（无文字）'}{note}")
        elif m["role"] == "BUYER":
            note = f"\n（该消息带 {old_counts[m['id']]} 个附件，内容未提供给你）" if old_counts.get(m["id"]) else ""
            history.append(HumanMessage(content=f"[买家历史消息 {m['id']} @ {m['at']}]\n{m['text']}{note}"))
        elif m["role"] == "SELLER":
            history.append(AIMessage(content=m["text"]))
        else:
            history.append(HumanMessage(content=f"[eBay 系统消息 {m['id']} @ {m['at']}]\n{m['text']}"))

    observations = [
        {k: o[k] for k in ("tool_name", "status", "evidence_ref", "data", "message", "queried_at")}
        for o in state["observations"]
    ]
    task = "\n".join([
        "<runtime_context>",
        f"店铺：{deps.store.name}（{run.store_id}）；站点：{run.marketplace_id}；店铺当地时间：{_store_now(deps)}",
        *([f"当前商品：{run.item_id}（{state['snapshot'].get('conversation_title') or '标题未知'}）"] if run.item_id else []),
        *([f"本店售后邮箱：{deps.settings.aftersales_public_email}（买家需要发视频或大文件时可以提供，请买家在邮件主题写上订单号）"]
          if deps.settings.aftersales_public_email else []),
        *(["物流查件：包裹轨迹多天未更新、轨迹异常、或已签收但买家说没收到时，系统会自动向仓库和承运商发起查件。"
           "这时告诉买家我们已经联系承运商核实包裹情况，有结果会第一时间通知；不给具体时间，不承诺退款、补发或赔偿"]
          if deps.settings.logistics_ticket_auto_submit and deps.settings.goodcang_ready else []),
        "</runtime_context>",
        "<tool_observations>",
        json.dumps(observations, ensure_ascii=False, default=str),
        "</tool_observations>",
        "<company_knowledge>",
        *(render_clause(c) for c in clauses),
        "</company_knowledge>",
        *_attachments_block(state),
        "<current_task>",
        "本次需要回复的买家消息（按时间顺序，全部都要回应）：",
        *task_lines,
        "</current_task>",
    ])

    template = ChatPromptTemplate.from_messages([
        ("system", "{system}"),
        MessagesPlaceholder("history"),
        ("human", "{task}"),
    ])
    return template.format_messages(system=system, history=history, task=task)


@step
def compose_reply(state: AgentState, deps: AgentDeps):
    problems = list(state["problems"])
    if deps.llm is None:
        problems.append("AGENT_NOT_READY")
    if any(p in problems for p in ("AGENT_NOT_READY", "RAG_NOT_READY", "KNOWLEDGE_OVER_BUDGET", "CONTEXT_OVER_BUDGET")):
        return {"skip_generation": True, "problems": problems, "_status": "SKIPPED"}

    messages = build_messages(state, deps)
    prompt_tokens = sum(estimate_tokens(str(m.content)) for m in messages)
    if prompt_tokens > deps.settings.input_token_budget:
        problems.append("CONTEXT_OVER_BUDGET")
        return {"skip_generation": True, "problems": problems, "_status": "SKIPPED",
                "_detail": {"prompt_tokens_est": prompt_tokens, "budget": deps.settings.input_token_budget}}
    provider, model = usage_log.chat_model(deps.settings)
    where = {"store_id": deps.run.store_id, "conversation_id": deps.run.conversation_id, "job_id": deps.run.job_id}

    def record(started, *, usage=None, ok=True, error=None):
        usage_log.record(deps.db, "reply", provider=provider, model=model, usage=usage, ok=ok, error=error,
                         duration_ms=int((time.perf_counter() - started) * 1000), **where)

    def final_answer(msgs):
        """不再调用工具，直接按输出契约作答。"""
        started = time.perf_counter()
        try:
            res = deps.llm.structured(msgs, ReplyDraftOutput)
        except Exception as e:
            record(started, ok=False, error=f"{type(e).__name__}: {e}")
            return None, e
        record(started, usage=res.usage, ok=res.parsed is not None, error=res.error)
        return res, None

    observations = list(state["observations"])
    usage: dict = {}
    calls: list[dict] = []  # 模型调用了哪些工具（给“生成依据”展示）
    rounds = 0
    r = err = None
    if deps.settings.agent_tool_calling and deps.llm.supports_tools:
        # 原生工具调用：模型自己决定查什么，一轮可以调多个工具；最终仍按输出契约给 JSON
        tools = model_tool_specs(has_item=bool(deps.run.item_id))
        ex = ToolExecutor(deps.db, deps.run, deps.source, deps.settings.agent_max_tool_requests)
        msgs = deps.llm.with_answer_schema(messages, ReplyDraftOutput)
        while True:
            started = time.perf_counter()
            try:
                resp, round_usage = deps.llm.tool_round(msgs, tools)
            except Exception as e:
                record(started, ok=False, error=f"{type(e).__name__}: {e}")
                err = e
                break
            record(started, usage=round_usage)
            usage = {k: (usage.get(k) or 0) + (round_usage.get(k) or 0) for k in set(usage) | set(round_usage)}
            if not resp.tool_calls:
                r = deps.llm.parse(str(resp.content), ReplyDraftOutput, usage)
                break
            rounds += 1
            msgs.append(resp)
            exhausted = False
            for tc in resp.tool_calls:
                name, args = tc["name"], tc.get("args") or {}
                if name not in MODEL_TOOL_DESCRIPTIONS:
                    content = json.dumps({"status": "ERROR", "message": f"没有 {name} 这个工具"}, ensure_ascii=False)
                else:
                    try:
                        res = ex.execute(name, args, request_id=f"model-{len(calls) + 1}")
                    except ToolBudgetExceeded:
                        exhausted = True
                        content = json.dumps({"status": "ERROR", "message": "工具调用次数已用完，请根据已有信息直接作答"},
                                             ensure_ascii=False)
                    else:
                        dumped = res.model_dump()
                        observations.append(dumped)
                        calls.append({"tool": name, "args": args, "status": res.status})
                        if res.status == "ERROR":
                            problems.append("TOOL_ERROR")
                        content = json.dumps({k: dumped[k] for k in ("status", "evidence_ref", "data", "message")},
                                             ensure_ascii=False, default=str)
                msgs.append(ToolMessage(content=content, tool_call_id=tc["id"]))
            if exhausted or rounds >= deps.settings.agent_max_tool_rounds or deps.time_left() < 10:
                # 轮数、次数或时间用完：带着已查到的结果直接作答
                r, err = final_answer(build_messages({**state, "observations": observations}, deps))
                if r:
                    usage = {k: (usage.get(k) or 0) + (r.usage.get(k) or 0) for k in set(usage) | set(r.usage)}
                    r.usage = usage
                break
    else:
        r, err = final_answer(messages)
        usage = r.usage if r else {}

    detail = {"prompt_tokens_est": prompt_tokens, "model": deps.settings.llm_display_name,
              "tool_rounds": rounds, "tool_calls": calls}
    if err is not None:
        problems.append("LLM_ERROR")
        return {"skip_generation": True, "problems": problems, "observations": observations, "_status": "ERROR",
                "_detail": {**detail, "error": f"{type(err).__name__}: {str(err)[:300]}"}}
    if deps.time_left() < 0:
        problems.append("DEADLINE_EXCEEDED")
    return {
        "raw_output": r.raw_text,
        "parsed": r.parsed.model_dump() if r.parsed else None,
        "parse_error": r.error,
        "usage": usage,
        "observations": observations,
        "problems": problems,
        "_status": "OK" if r.parsed else "PARSE_ERROR",
        "_detail": {**detail, "error": r.error[:300] if r.error else None},
    }


@step
def repair_output(state: AgentState, deps: AgentDeps):
    messages = build_messages(state, deps)
    provider, model = usage_log.chat_model(deps.settings)
    where = {"store_id": deps.run.store_id, "conversation_id": deps.run.conversation_id, "job_id": deps.run.job_id}
    started = time.perf_counter()
    try:
        r = deps.llm.repair(messages, ReplyDraftOutput, state.get("raw_output", ""), state.get("parse_error") or "",
                            state.get("usage") or {})
    except Exception as e:
        usage_log.record(deps.db, "repair", provider=provider, model=model, ok=False, error=f"{type(e).__name__}: {e}",
                         duration_ms=int((time.perf_counter() - started) * 1000), **where)
        return {"repairs": state["repairs"] + 1, "problems": [*state["problems"], "LLM_ERROR"],
                "skip_generation": True, "_status": "ERROR", "_detail": {"error": str(e)[:300]}}
    usage_log.record(deps.db, "repair", provider=provider, model=model,
                     usage=usage_log.usage_delta(r.usage, state.get("usage")), ok=r.parsed is not None, error=r.error,
                     duration_ms=int((time.perf_counter() - started) * 1000), **where)
    return {
        "repairs": state["repairs"] + 1,
        "raw_output": r.raw_text,
        "parsed": r.parsed.model_dump() if r.parsed else None,
        "parse_error": r.error,
        "usage": r.usage,
        "_status": "OK" if r.parsed else "PARSE_ERROR",
    }


# ---------------------------------------------------------------- 校验与闸门
def _evidence(state: AgentState, deps: AgentDeps) -> tuple[set[str], str, set[str]]:
    """本轮允许引用的证据 ID、证据原文语料（用于数字核对）、允许出现的单号类 token。"""
    allowed = set(state["knowledge"]["versions"])
    corpus_parts: list[str] = []
    tokens: set[str] = set()
    for o in state["observations"]:
        if o["status"] in ("FOUND", "AMBIGUOUS") and o.get("evidence_ref"):
            allowed.add(o["evidence_ref"])
            corpus_parts.append(json.dumps(o["data"], ensure_ascii=False, default=str))
            for row in o["data"] if isinstance(o["data"], list) else [o["data"]]:
                if isinstance(row, dict):
                    tokens.update(str(row[k]) for k in ("order_id", "tracking_number", "case_id", "item_id") if row.get(k))
    for a in state.get("attachments") or []:
        if a["status"] == "OBSERVED":
            allowed.add(a["evidence_ref"])
            corpus_parts.append(json.dumps(a["observation"], ensure_ascii=False))
    clauses = deps.db.scalars(
        select(KnowledgeChunk.text).where(KnowledgeChunk.chunk_id.in_(state["knowledge"]["clause_ids"]))
    ).all()
    corpus_parts.extend(clauses)
    return allowed, "\n".join(corpus_parts), tokens


def _open_cases(state: AgentState) -> list[dict]:
    cases = []
    for o in state["observations"]:
        if o["tool_name"] == "get_case_status" and o["status"] == "FOUND":
            cases.extend(o["data"])
    return cases


_NOT_RECEIVED = re.compile(
    r"not (yet )?(been )?(received|arrived|delivered)|never (arrived|received|came)|haven'?t (got|received)|"
    r"didn'?t (get|receive|arrive)|nicht (angekommen|erhalten|bekommen|zugestellt)|nie angekommen|"
    r"pas (re[çc]u|arriv)|non (ho )?ricevut|no (he )?recibido|no ha llegado", re.I)


# 损坏、丢件、退回、空包裹、轨迹未更新/长时间没更新、买家要求补发 → 需判断补发；其余物流问题 → 物流异常/未收到
RESHIP_TYPE = "物流问题需判断补发"
_LOGISTICS_HUMAN = {"TRACKING_RESHIP_REVIEW": RESHIP_TYPE, "RESHIP_REQUESTED": RESHIP_TYPE, "TRACKING_STALLED": RESHIP_TYPE,
                    "TRACKING_LOST_DAMAGED": RESHIP_TYPE, "EMPTY_PARCEL": RESHIP_TYPE,
                    "TRACKING_NOT_FOUND": "物流异常/未收到", "TRACKING_EXCEPTION": "物流异常/未收到",
                    "TRACKING_DELIVERED_NOT_RECEIVED": "物流异常/未收到"}


def _mark_logistics_aftersales(db, job, state: AgentState, reasons: list[str]) -> str | None:
    """物流需要人工时，把会话自动标成“待售后处理”（已在售后处理中的不动），备注写清原因，客服在工作台看到。"""
    hits = [r for r in reasons if r in _LOGISTICS_HUMAN]
    if not hits:
        return None
    conv = db.get(Conversation, job.conversation_id)
    if conv is None or conv.aftersales_status in (Aftersales.PENDING, Aftersales.IN_PROGRESS):
        return None
    kind = next((_LOGISTICS_HUMAN[r] for r in hits if _LOGISTICS_HUMAN[r] == RESHIP_TYPE), _LOGISTICS_HUMAN[hits[0]])
    lines = [risk.REASON_LABELS.get(r, r) for r in hits]
    for o in state["observations"]:
        if o["tool_name"] == "get_package_status" and isinstance(o.get("data"), (dict, list)):
            for r in (o["data"] if isinstance(o["data"], list) else [o["data"]]):
                a = (r or {}).get("assessment") or {}
                if a.get("needs_human"):
                    days = f"，{a['days_without_update']} 天未更新" if a.get("days_without_update") is not None else ""
                    lines.append(f"{r.get('carrier') or ''} {r.get('tracking_number') or '无跟踪号'}：{a.get('stage_label')}{days}")
    now = utcnow()
    conv.aftersales_status, conv.aftersales_type = Aftersales.PENDING, kind
    conv.aftersales_note = "；".join(dict.fromkeys(lines))[:1000]
    conv.aftersales_marked_by, conv.aftersales_marked_at, conv.aftersales_handled_by = "AI（物流规则）", now, None
    conv.aftersales_updated_at = now
    db.add(AuditEvent(actor="agent", action="AFTERSALES_AUTO_MARKED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"type": kind, "reasons": hits, "job_id": job.id}))
    return kind


_RESHIP = re.compile(
    r"\b(resend|re-send|reship|replacement|send (me )?(a )?new|send (it )?again)\b|ersatz|neu (zu)?senden|"
    r"nochmal (zu)?schicken|erneut (zu)?senden|neue lieferung|renvoy|nouvel envoi|rispedi|nuovo invio|reenv|"
    r"补发|重发|再发", re.I)


_EMPTY_PARCEL = re.compile(
    r"\bempty\s+(box|package|parcel|carton)\b|(box|package|parcel)\s+(was|is|came|arrived)\s+empty|nothing\s+(was\s+)?inside|"
    r"leere?[sn]?\s+(paket|karton|packung|schachtel)|paket\s+(war|ist|kam)\s+leer|nichts\s+(drin|darin|enthalten)|"
    r"colis\s+vide|carton\s+vide|pacco\s+vuoto|scatola\s+vuota|paquete\s+vac[ií]o|caja\s+vac[ií]a|空包|空箱|空盒|里面没有东西|什么都没有", re.I)


def _tracking_problems(state: AgentState, buyer_text: str) -> list[str]:
    """物流需要人工的情况（规则见 logistics/assess.py）：查不到、异常、断更、超时需判断补发、已签收但买家说没收到；
    买家自己提出补发、重发、换货也交人工。"""
    out = []
    if _RESHIP.search(buyer_text or ""):
        out.append("RESHIP_REQUESTED")
    if _EMPTY_PARCEL.search(buyer_text or ""):
        out.append("EMPTY_PARCEL")
    for o in state["observations"]:
        if o["tool_name"] != "get_package_status" or not isinstance(o.get("data"), (dict, list)):
            continue
        rows = o["data"] if isinstance(o["data"], list) else [o["data"]]
        for r in (x for x in rows if isinstance(x, dict)):
            a, t = r.get("assessment") or {}, r.get("tracking") or {}
            if a.get("reason_code"):
                out.append(a["reason_code"])
            elif t.get("status") in TRACK_RISKY_STATUS:
                out.append("TRACKING_EXCEPTION")
            if t.get("status") == "Delivered" and _NOT_RECEIVED.search(buyer_text or ""):
                out.append("TRACKING_DELIVERED_NOT_RECEIVED")
    return list(dict.fromkeys(out))


@step
def validate_output(state: AgentState, deps: AgentDeps):
    problems = list(state["problems"])
    parsed = state.get("parsed")
    if not state.get("skip_generation") and parsed is None:
        if state["repairs"] < deps.settings.agent_max_output_repairs:
            return {"route": "repair", "_status": "NEEDS_REPAIR"}
        problems.append("OUTPUT_INVALID")

    snap = state["snapshot"]
    atts = state.get("attachments") or []
    compliance = None
    kb_cited: set[str] = set()
    listing_cited: set[str] = set()
    other_cited: set[str] = set()
    if parsed and language_mismatch((snap["buyer_texts"] or [""])[-1], parsed["reply_text"]):
        problems.append("LANGUAGE_MISMATCH")
    # 发票场景里买家发来了邮箱：登记发票申请，这次回复交人工（开票、审核后发邮件）
    invoice_email = detect_invoice_email(
        snap["buyer_texts"], [m["text"] or "" for m in state["memory"]["messages"] if not m["in_task"]],
        parsed["intents"] if parsed else [])
    if invoice_email:
        problems.append("INVOICE_EMAIL_RECEIVED")
    problems += _tracking_problems(state, "\n".join(snap["buyer_texts"]))
    if parsed:
        allowed, corpus, tokens = _evidence(state, deps)
        kb_cited = {e for c in parsed["claims"] for e in c["evidence_ids"] if e.startswith("kb:") and e in allowed}
        listing_cited = {e for c in parsed["claims"] for e in c["evidence_ids"]
                         if e.startswith("listing:") and e in allowed}
        other_cited = {e for c in parsed["claims"] for e in c["evidence_ids"]
                       if e in allowed and not e.startswith(("kb:", "listing:"))}
        problems += check_claims(parsed["reply_text"], parsed["claims"], allowed, corpus,
                                 parsed["action"], parsed["intent"])
        compliance = ComplianceChecker().check(parsed["reply_text"], allowed_tokens=tokens).to_dict()

    gate = risk.evaluate(risk.GateInput(
        intents=parsed["intents"] if parsed else [],
        buyer_text="\n".join(snap["buyer_texts"]),
        open_cases=_open_cases(state),
        handoff_state=snap["handoff_state"],
        marketplace_id=snap["marketplace_id"],
        action=parsed["action"] if parsed else None,
        needs_human=parsed["needs_human"] if parsed else False,
        missing_facts=parsed["missing_facts"] if parsed else [],
        conflicts=parsed["conflicts"] if parsed else [],
        problems=problems,
        compliance_result=compliance["result"] if compliance else "PASS",
        auto_send_enabled=deps.settings.auto_send_enabled,
        store_auto_send_enabled=deps.store.auto_send_enabled,
        auto_send_intents=deps.settings.auto_send_intent_set,
        auto_clarify_intents=deps.settings.auto_send_clarify_intent_set,
        kb_evidence=len(kb_cited),
        listing_evidence=len(listing_cited),
        auto_listing_intents=deps.settings.auto_send_listing_intent_set,
        previous_auto_reply=auto_reply_streak(deps.db, deps.run.conversation_id)
        >= max(1, deps.settings.auto_reply_max_streak),
        auto_max_risk=deps.settings.auto_send_max_risk if deps.settings.auto_send_max_risk in ("L0", "L1") else "L0",
        auto_exclude_intents=deps.settings.auto_send_exclude_intent_set,
        evidence_mode=deps.settings.auto_send_evidence,
        other_evidence=len(other_cited),
        claims=len(parsed["claims"]) if parsed else 0,
        attachments=len(atts),
        attachments_not_read=sum(a["status"] != "OBSERVED" for a in atts),
        image_damage=any((a.get("observation") or {}).get("damage_observed") for a in atts),
    ))
    result = {
        "has_draft": parsed is not None,
        "draft": parsed,
        "compliance": compliance,
        "risk_level": gate.risk_level,
        "decision": gate.decision,
        "reasons": gate.reasons,
        "rule_version": gate.rule_version,
        "invoice_email": invoice_email,
    }
    return {"route": "persist", "result": result, "problems": problems,
            "_detail": {"decision": gate.decision, "risk": gate.risk_level, "reasons": gate.reasons}}


# ---------------------------------------------------------------- 落库
@step
def persist_candidate(state: AgentState, deps: AgentDeps):
    """幂等落库：同一 job + generation 只保存一份草稿；任务已被新消息作废则丢弃本次结果。"""
    db, run = deps.db, deps.run
    job = db.get(ReplyJob, run.job_id)
    db.refresh(job)
    if job.state != JobState.RUNNING or job.run_generation != run.run_generation:
        return {"_status": "STALE_DISCARDED", "_detail": {"job_state": job.state, "generation": job.run_generation}}

    res = state["result"]
    existing = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == run.run_generation))
    if res["has_draft"] and existing is None:
        for d in db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")):
            d.status = "SUPERSEDED"
        out = res["draft"]
        version = (db.scalar(select(func.max(Draft.version)).where(Draft.job_id == job.id)) or 0) + 1
        db.add(Draft(
            job_id=job.id,
            run_generation=run.run_generation,
            version=version,
            author="agent",
            conversation_row_version=state["snapshot"]["conversation_row_version"],
            text=out["reply_text"],
            text_zh=out.get("reply_zh") or None,
            reasoning_zh=out.get("reasoning_zh") or None,
            language=out["language"],
            intent=out["intent"],
            intents=out["intents"],
            action=out["action"],
            risk_level=res["risk_level"],
            decision=res["decision"],
            claims=out["claims"],
            evidence_ids=sorted({e for c in out["claims"] for e in c["evidence_ids"]}),
            missing_facts=out["missing_facts"],
            conflicts=out["conflicts"],
            review_reasons=res["reasons"],
            compliance_result=res["compliance"],
            compliance_rules_version=(res["compliance"] or {}).get("rules_version"),
            prompt_version=prompt_version(*PROMPT_NAMES),
            model_config_version=deps.settings.model_config_version(),
            knowledge_versions=state["knowledge"]["versions"],
            token_usage=state.get("usage") or None,
        ))

    job.state = JobState.AUTO_ELIGIBLE if res["decision"] == "AUTO_ELIGIBLE" else JobState.REVIEW_REQUIRED
    job.risk_level = res["risk_level"]
    job.decision_reasons = res["reasons"]
    job.rule_version = res["rule_version"]
    job.model_version = deps.settings.model_config_version()
    job.last_error = None
    job.lock_version += 1
    detail = {"has_draft": res["has_draft"]}
    if res.get("invoice_email"):
        detail["invoice_request"] = _invoice_request(db, job, state, res["invoice_email"])
    elif res["has_draft"] and "invoice_vat" in (res["draft"].get("intents") or []):
        detail["invoice_request"] = _invoice_waiting(db, job, state)
    try:  # 谷仓查件单（起草、按配置直接提交）、补发库存：失败只记录，不影响草稿落库
        with db.begin_nested():
            follow = logistics_followup.after_reply(db, job, db.get(Conversation, job.conversation_id), state, res["reasons"])
    except Exception as e:  # noqa: BLE001
        log.exception("物流跟进失败 job=%s", job.id)
        follow = {"error": type(e).__name__}
    if follow:
        detail["logistics_followup"] = follow
    if res["has_draft"]:  # 按问题类型自动打标记：失败不影响草稿落库
        try:
            with db.begin_nested():
                tagged = auto_tags.apply(db, db.get(Conversation, job.conversation_id), res["draft"].get("intents") or [])
            if tagged:
                detail["auto_tags"] = tagged
        except Exception:  # noqa: BLE001
            log.exception("自动打标记失败 job=%s", job.id)
    handled = _apply_logistics_auto(db, job, run.run_generation, res, follow)
    if handled:
        detail["logistics_auto_handled"] = sorted(handled)
    marked = _mark_logistics_aftersales(db, job, state, [r for r in res["reasons"] if r not in handled])
    if marked:
        detail["aftersales_marked"] = marked
    if job.state == JobState.AUTO_ELIGIBLE and res["has_draft"]:
        detail["auto_send"] = _auto_send(db, job)
        if detail["auto_send"] == "SENT":
            return {"_status": "AUTO_SENT", "_detail": detail}
    return {"_status": job.state, "_detail": detail}


def _apply_logistics_auto(db, job: ReplyJob, generation: int, res: dict, follow: dict | None) -> set[str]:
    """查件单已自动提交（或已有在跟的）：这些物流原因不用人工审核，告诉买家“已发起查件”的回复照常走自动发送。
    其他原因（店铺开关、合规、风险等）照旧。返回去掉的原因。"""
    handled = logistics_followup.auto_handled(follow) & set(res["reasons"])
    if not handled:
        return set()
    left = [r for r in res["reasons"] if r not in handled]
    job.decision_reasons = left
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == generation))
    if draft is not None:
        draft.review_reasons = left
    if not left and res["has_draft"]:
        job.state = JobState.AUTO_ELIGIBLE
        if draft is not None:
            draft.decision = "AUTO_ELIGIBLE"
    return handled


def _order_candidates(state: AgentState) -> list[str]:
    """预取的买家订单：一笔就是它，多笔让人工选。"""
    for o in state["observations"]:
        if o["tool_name"] != "get_authorized_order_context":
            continue
        if o["status"] == "FOUND" and isinstance(o.get("data"), dict):
            return [o["data"]["order_id"]]
        if o["status"] == "AMBIGUOUS" and isinstance(o.get("data"), list):
            return [r["order_id"] for r in o["data"] if r.get("order_id")]
    return []


def _invoice_request(db, job: ReplyJob, state: AgentState, email: str) -> str:
    try:
        with db.begin_nested():
            req = record_request(db, db.get(Conversation, job.conversation_id), email,
                                 order_candidates=_order_candidates(state), buyer_texts=state["snapshot"]["buyer_texts"])
        return "INVOICE_READY" if req.order_id and not req.note else "NEEDS_ORDER"
    except Exception:  # noqa: BLE001 — 登记失败不影响草稿落库，人工仍可在会话里手动开票
        log.exception("登记发票申请失败")
        return "FAILED"


def _invoice_waiting(db, job: ReplyJob, state: AgentState) -> str:
    """买家要发票但还没给邮箱：先在发票页登记“等买家邮箱”。"""
    try:
        with db.begin_nested():
            req = ensure_waiting(db, db.get(Conversation, job.conversation_id), order_candidates=_order_candidates(state),
                                 buyer_texts=state["snapshot"]["buyer_texts"])
        return "WAITING_EMAIL" if req else "EXISTS"
    except Exception:  # noqa: BLE001
        log.exception("登记发票申请失败")
        return "FAILED"


def _auto_send(db, job: ReplyJob) -> str:
    """通过闸门（含店铺开关、全局开关、知识库依据）的草稿自动发出。只读阶段仍是模拟发送，不调用 eBay。

    有人正在处理这个会话（认领未过期）时不抢着发；发送前的复核（会话有新消息、合规）没过就留在待审队列。
    """
    from ..models import WorkClaim

    conv = db.get(Conversation, job.conversation_id)
    claim = db.scalar(select(WorkClaim).where(WorkClaim.store_id == conv.store_id,
                                              WorkClaim.conversation_id == conv.id))
    if claim and claim.expires_at > utcnow():
        return "SKIPPED_CLAIMED"
    db.flush()
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")
                      .order_by(Draft.id.desc()).limit(1))
    try:
        with db.begin_nested():
            simulate_send(db, conv=conv, text=draft.text, actor=AUTO_ACTOR, auto=True)
    except SendError as e:
        job.last_error = f"自动发送未执行：{e.message}"[:2000]
        return "SKIPPED_" + str(e.status_code)
    return "SENT"
