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
from datetime import datetime, timedelta, timezone
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
from ..invoicing.service import INVOICE_WORDS, detect_invoice_email, ensure_waiting, record_request, suggest_type
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
    InvoiceRequestStatus,
    JobState,
    KnowledgeChunk,
    LogisticsTicket,
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
        "latest_buyer_at": max((m.sent_at.isoformat() for m in buyer_msgs if m.sent_at), default=None),
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
        *([f"本店售后邮箱：{deps.settings.aftersales_public_email}（买家需要发视频或大文件时可以提供，"
           "请买家在邮件主题写上 eBay 用户名或订单号；视频太大的用 WeTransfer 发到这个邮箱）"]
          if deps.settings.aftersales_public_email else []),
        *_video_link_context(state, deps),
        *(["物流查件：包裹轨迹多天未更新、轨迹异常、或已签收但买家说没收到时，系统会自动向仓库和承运商发起查件。"
           "这时告诉买家我们已经联系承运商核实包裹情况，有结果会第一时间通知；不给具体时间，不承诺退款、补发或赔偿"]
          if deps.settings.logistics_ticket_auto_submit and deps.settings.goodcang_ready else []),
        *_invoice_context(state, deps),
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
    from ..customers import blacklist_entry

    if blacklist_entry(deps.db, deps.run.store_id, deps.run.buyer_key):
        # 黑名单买家：AI 不起草回复，直接交人工
        return {"skip_generation": True, "problems": [*problems, "BUYER_BLACKLISTED"], "_status": "SKIPPED",
                "_detail": {"reason": "黑名单买家"}}
    if state["snapshot"].get("handoff_state") == "HUMAN":
        # 已人工接管：AI 不再生成回复，新来信直接进待审核由客服处理
        return {"skip_generation": True, "problems": [*problems, "HUMAN_HANDOFF"], "_status": "SKIPPED",
                "_detail": {"reason": "已人工接管"}}
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


# 买家说没收到（配合“轨迹已签收”判 TRACKING_DELIVERED_NOT_RECEIVED，进而起草“妥投未收到”查件单、标售后）。
# 三类说法分开判，避免把“没收到发票 / 找不到安装说明 / 没收到回复”当成没收到包裹：
# 1. 说法本身就点名了包裹（Paket ist weg、can't find my parcel……）：算；
# 2. “什么都没收到”（nichts bekommen、rien reçu……）：算，除非这句只提到了别的东西（发票、回复、退款……）而没提包裹；
# 3. 宽泛说法（nicht bekommen、haven't received、never received……）：前后几个词里要有包裹 / 货物 / 订单类的词，
#    而且它比“别的东西”离得更近（“Paket nicht bekommen und auch keine Rechnung”算，“die Rechnung nicht bekommen”不算）。
_NR_PACKAGE = re.compile(
    r"(paket|päckchen|sendung|lieferung|ware|artikel)\W+(\w+\W+){0,3}(ist )?(nicht da|nicht hier|nirgends|weg)\b|"
    r"kein (paket|päckchen)|"
    r"(don['’]?t|do not) have (the|my|any) (package|parcel|item|order)|"
    r"(can['’]?t|cannot|could not|couldn['’]?t) find (the|my|any) (package|parcel)|"
    r"no (package|parcel) (arrived|here|was delivered)|(package|parcel) (is )?(not|isn['’]?t) here|"
    r"(colis|paquet) (n['’]est )?pas l[àa]", re.I)
_NR_NOTHING = re.compile(
    r"nichts (bekommen|erhalten|angekommen)|nie angekommen|nothing (arrived|came|was delivered)|"
    r"rien re[çc]u|non [èe] arrivato niente|no (he )?recibido nada|no me ha llegado", re.I)
# 德语宾语在动词前：“die Rechnung nicht bekommen”看前面的名词，只往前找
_NR_BROAD_DE = re.compile(r"nicht (angekommen|erhalten|bekommen|zugestellt|gefunden|auffindbar)", re.I)
# 德语代词紧跟“nicht bekommen”（中间可以有 noch、leider 等）：“ich habe es nicht bekommen”；“das Geld nicht bekommen”里的 das 是冠词，不算
_NR_DE_PRONOUN = re.compile(
    r"\b(es|ihn|sie|das|nichts)\s+((leider|noch|bisher|immer|jetzt|bis heute)\s+){0,3}nicht (bekommen|erhalten|angekommen)\b", re.I)
_NR_BROAD = re.compile(
    r"not (yet )?(been )?(received|arrived|delivered)|never (arrived|received|came|got)|haven['’]?t (yet )?(got|received)|"
    r"(hasn['’]?t|has not|haven['’]?t|have not) (yet )?(arrived|come|been delivered)|"
    r"didn['’]?t (get|receive|arrive)|pas (re[çc]u|arriv)|non (ho )?ricevut|no (he )?recibido|no ha llegado", re.I)
_NR_PARCEL = re.compile(
    r"\b(paket|päckchen|sendung|lieferung|bestellung|ware|artikel|teil|teile|package|parcel|order|item|items|it|shipment|"
    r"delivery|goods|colis|paquet|commande|pacco|ordine|paquete|pedido)\b", re.I)
_NR_OTHER = re.compile(
    r"rechnung|anleitung|antwort|erstattung|gutschrift|trackingnummer|sendungsnummer|nachricht|"
    r"invoice|receipt|manual|instructions|reply|answer|refund|tracking ?number|message|e-?mail|"
    r"facture|remboursement|fattura|rimborso|factura|reembolso", re.I)
# 只用于“要不要先查包裹”（多查一次没坏处），宽松；判“妥投未收到”用 buyer_says_not_received
OLD_MESSAGE_AGE = timedelta(hours=24)  # 与同步里店铺 AI 开关的时效一致
_NOT_RECEIVED = re.compile("|".join(r.pattern for r in (_NR_PACKAGE, _NR_NOTHING, _NR_BROAD_DE, _NR_BROAD)), re.I)


def _nearest(rx: re.Pattern, t: str, m: re.Match, before_only: bool) -> int | None:
    """rx 在 m 前后 60 字内离 m 最近的距离（字符数）；before_only 只看前面。"""
    lo, hi = max(0, m.start() - 60), (m.start() if before_only else min(len(t), m.end() + 60))
    ds = [min(abs(x.end() + lo - m.start()), abs(x.start() + lo - m.end())) for x in rx.finditer(t[lo:hi])]
    return min(ds) if ds else None


def buyer_says_not_received(text: str | None) -> bool:
    """买家说的是“包裹没收到 / 找不到”（不是发票、回复、退款、说明书没收到）。"""
    t = text or ""
    if _NR_PACKAGE.search(t) or _NR_DE_PRONOUN.search(t):
        return True
    if _NR_NOTHING.search(t) and (not _NR_OTHER.search(t) or _NR_PARCEL.search(t)):
        return True
    for rx, before_only in ((_NR_BROAD_DE, True), (_NR_BROAD, False)):
        for m in rx.finditer(t):
            parcel, other = _nearest(_NR_PARCEL, t, m, before_only), _nearest(_NR_OTHER, t, m, before_only)
            if parcel is not None and (other is None or parcel <= other):
                return True
    return False


# 损坏、丢件、退回、空包裹、轨迹未更新/长时间没更新、买家要求补发 → 需判断补发；其余物流问题 → 物流异常/未收到
RESHIP_TYPE = "物流问题需判断补发"
_LOGISTICS_HUMAN = {"TRACKING_RESHIP_REVIEW": RESHIP_TYPE, "RESHIP_REQUESTED": RESHIP_TYPE, "TRACKING_STALLED": RESHIP_TYPE,
                    "TRACKING_LOST_DAMAGED": RESHIP_TYPE, "EMPTY_PARCEL": RESHIP_TYPE,
                    "TRACKING_NOT_FOUND": "物流异常/未收到", "TRACKING_EXCEPTION": "物流异常/未收到",
                    "TRACKING_DELIVERED_NOT_RECEIVED": "物流异常/未收到", "TRACKING_PICKUP_OVERDUE": "物流异常/未收到",
                    "SHIP_LATE": "延迟发货"}


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


def _ship_late(state: AgentState) -> bool:
    """超过 eBay 承诺的最晚发货日还没发货（订单未发货/部分发货，且查包裹没有任何跟踪号或谷仓还在待发货）。"""
    order = next((o["data"] for o in state["observations"] if o["tool_name"] == "get_authorized_order_context"
                  and o["status"] == "FOUND" and isinstance(o.get("data"), dict)), None)
    if not order or order.get("order_status") not in ("NOT_STARTED", "IN_PROGRESS") or not order.get("ship_by_date"):
        return False
    try:
        due = datetime.fromisoformat(order["ship_by_date"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if due >= datetime.now(timezone.utc):
        return False
    for o in state["observations"]:
        if o["tool_name"] == "get_package_status" and isinstance(o.get("data"), (dict, list)):
            rows = o["data"] if isinstance(o["data"], list) else [o["data"]]
            if any(isinstance(r, dict) and r.get("tracking_number") and (r.get("assessment") or {}).get("stage") != "NOT_SHIPPED"
                   for r in rows):
                return False  # 已经有在途的包裹（eBay 还没同步发货状态）
    return True


def _tracking_problems(state: AgentState, buyer_text: str) -> list[str]:
    """物流需要人工的情况（规则见 logistics/assess.py）：查不到、异常、断更、超时需判断补发、已签收但买家说没收到；
    买家自己提出补发、重发、换货也交人工。"""
    out = []
    if _RESHIP.search(buyer_text or ""):
        out.append("RESHIP_REQUESTED")
    if _EMPTY_PARCEL.search(buyer_text or ""):
        out.append("EMPTY_PARCEL")
    if _ship_late(state):
        out.append("SHIP_LATE")
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
            delivered = t.get("status") == "Delivered" or a.get("stage") == "DELIVERED"  # 含谷仓回传妥投
            if delivered and buyer_says_not_received(buyer_text):
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
    # 发票：买家要增值税发票的转人工；普通发票买家给了邮箱时，开了自动发送就自动开票发出（提示词里已告诉模型），否则交人工
    fresh_texts, other_texts = _invoice_texts(state, deps)  # 已经开票发出过的来信不再触发开票
    invoice_email = detect_invoice_email(fresh_texts, other_texts, parsed["intents"] if parsed else [])
    inv_mode = _invoice_mode(state, deps, parsed["intents"] if parsed else [])
    if not invoice_email and inv_mode == "AUTO_SEND" and parsed and _asks_invoice_again(state, deps, parsed["intents"]):
        invoice_email = _remembered_invoice_email(state)  # 买家再要一次发票、这次没写邮箱：发到他之前给过的邮箱
    # 订单唯一才会真的自动开票发出；多笔订单或查不到订单时回复是请买家给订单号（提示词里已说明），不算失败
    invoice_orders = len(_order_candidates(state))
    invoice_auto = bool(invoice_email) and inv_mode == "AUTO_SEND" and invoice_orders == 1
    if inv_mode == "VAT":
        problems.append("INVOICE_VAT_HUMAN")
    elif invoice_auto:
        pass  # 订单唯一：落库时自动开票发出，不用审核
    elif invoice_email and inv_mode == "AUTO_SEND":
        # 订单不唯一：发票申请照样登记（备注“请选择订单”）。回复应是追问订单号（clarify）；模型没照做（比如说成“已发出”）就交人工
        if not parsed or parsed["action"] != "clarify":
            problems.append("INVOICE_NEEDS_ORDER")
    elif invoice_email:
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

    # 超过 24 小时的旧来信（补拉到的、首次同步补建的任务）：只起草，必须人工审核，不自动发
    latest = snap.get("latest_buyer_at")
    if latest and datetime.fromisoformat(latest) < utcnow() - OLD_MESSAGE_AGE:
        problems.append("OLD_MESSAGE")
    suppress = False  # 规则：会话最后一句永远是我们发的——不再因为“同事在跟进”压掉草稿，只标出来请跟进的同事核对后再发
    if parsed and parsed["action"] == "handoff" and _human_following(deps):
        problems.append("HUMAN_FOLLOWING")

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
        "invoice_auto": invoice_auto,
        "suppress_draft": suppress,
        "invoice_vat": inv_mode == "VAT",
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
    if res["has_draft"] and existing is None and not res.get("suppress_draft"):
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
        if res.get("invoice_auto") and detail["invoice_request"] != "AUTO_SENT":
            _force_review(db, job, run, res, "INVOICE_AUTO_FAILED")  # 回复里说了“已发出”，没发成就不能自动发
        elif detail["invoice_request"] == "AUTO_SENT":
            _invoice_grounded(db, job, run, res)
    elif res["has_draft"] and ("invoice_vat" in (res["draft"].get("intents") or []) or res.get("invoice_vat")):
        detail["invoice_request"] = _invoice_waiting(db, job, state)
    try:  # 谷仓查件单（起草、按配置直接提交）、补发库存：失败只记录，不影响草稿落库
        with db.begin_nested():
            follow = logistics_followup.after_reply(db, job, db.get(Conversation, job.conversation_id), state, res["reasons"])
    except Exception as e:  # noqa: BLE001
        log.exception("物流跟进失败 job=%s", job.id)
        follow = {"error": type(e).__name__}
    if follow:
        detail["logistics_followup"] = follow
    try:  # 异常包裹跟进表：物流需要人工的自动登记 / 补写（失败不影响草稿落库）
        from .. import parcel_auto

        with db.begin_nested():
            pf = parcel_auto.from_reply(db, db.get(Conversation, job.conversation_id), state, res["reasons"],
                                        follow if isinstance(follow, dict) else None)
        if pf:
            detail["parcel_followups"] = pf
    except Exception:  # noqa: BLE001
        log.exception("异常包裹跟进登记失败 job=%s", job.id)
    if res["has_draft"]:  # 会话情况标签：AI 看整段会话给出的阶段和一句话概括，列表里醒目显示
        conv = db.get(Conversation, job.conversation_id)
        stage, label = res["draft"].get("situation_stage"), (res["draft"].get("situation_zh") or "").strip()
        if conv is not None and (label or stage not in (None, "other")):
            conv.situation_stage, conv.situation_label, conv.situation_at = stage or "other", label or None, utcnow()
    if res["has_draft"]:  # 按问题类型自动打标记：失败不影响草稿落库
        try:
            with db.begin_nested():
                tagged = auto_tags.apply(db, db.get(Conversation, job.conversation_id), res["draft"].get("intents") or [])
            if tagged:
                detail["auto_tags"] = tagged
        except Exception:  # noqa: BLE001
            log.exception("自动打标记失败 job=%s", job.id)
        try:  # 需技术/售后判断的（质量问题、破损错发缺件、安装技术问题、发 VIN 核对适配）：起草飞书售后群工单（资料齐全且开了自动发送的直接发群）
            from ..feishu import cases as feishu_cases

            with db.begin_nested():
                case = feishu_cases.suggest(db, db.get(Conversation, job.conversation_id), state, res["draft"])
            if case is not None:
                detail["aftersales_case"] = {"id": case.id, "status": case.status, "category": case.category}
        except Exception:  # noqa: BLE001
            log.exception("起草售后群工单失败 job=%s", job.id)
    handled = _apply_logistics_auto(db, job, run.run_generation, res, follow)
    if handled:
        detail["logistics_auto_handled"] = sorted(handled)
    marked = _mark_logistics_aftersales(db, job, state, [r for r in res["reasons"] if r not in handled])
    if marked:
        detail["aftersales_marked"] = marked
        logistics_followup.append_note(db.get(Conversation, job.conversation_id), follow)  # 补发库存、查件单写进备注
    notice = _submit_notice(db, follow)
    if notice:
        detail["submit_notice"] = notice
        if notice == "SENT":  # 本次自动提交了查件单：安抚消息就是这次的回复，任务已结案
            return {"_status": "AUTO_SENT", "_detail": detail}
    if job.state == JobState.AUTO_ELIGIBLE and res["has_draft"]:
        detail["auto_send"] = _auto_send(db, job)
        if detail["auto_send"] == "SENT":
            return {"_status": "AUTO_SENT", "_detail": detail}
    return {"_status": job.state, "_detail": detail}


def _submit_notice(db, follow: dict | None) -> str | None:
    """本次回复流程里自动提交了查件单：给买家发固定的安抚消息（已转物流同事、已向承运商查询、有结果马上通知），
    代替 AI 草稿作为这次的回复；买家还问了别的或有其他需人工的原因时不发（见 followup.send_submit_notice）。"""
    codes = [t["io_code"] for t in (follow or {}).get("tickets") or [] if t.get("submitted")]
    if not codes:
        return None
    rows = db.scalars(select(LogisticsTicket).where(LogisticsTicket.io_code.in_(codes))).all()
    return logistics_followup.send_submit_notice(db, list(rows), respect_claim=True)


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


def _human_following(deps: AgentDeps) -> bool:
    """上一条店铺回复是不是同事人工发的、且在 HUMAN_FOLLOWUP_HOURS 内（工作台人工发送，或直接在 eBay 后台回复）。
    AI 自动回复、已收到回执、查件结果、发货通知等系统消息不算。"""
    from datetime import timedelta

    from ..models import OutboundMessage, SendIntent
    from ..sending.service import SIMULATED_PREFIX, is_ack

    hours = deps.settings.human_followup_hours
    if hours <= 0:
        return False
    db = deps.db
    last = db.scalar(select(Message).where(Message.conversation_id == deps.run.conversation_id,
                                           Message.sender_role == "SELLER", Message.auto_kind.is_(None))
                     .order_by(Message.conversation_seq.desc()).limit(1))
    if last is None or is_ack(last.ebay_message_id) or last.sent_at < utcnow() - timedelta(hours=hours):
        return False
    ob = db.scalar(select(OutboundMessage).where(OutboundMessage.platform_message_id == last.ebay_message_id).limit(1))
    if ob is not None:
        return ob.source == "manual"
    intent = db.scalar(select(SendIntent).where(SendIntent.platform_message_id == last.ebay_message_id).limit(1))
    if intent is not None:
        return intent.actor != AUTO_ACTOR
    # 不是本系统发的：同事直接在 eBay 后台回复的；本地模拟消息（查件、发货通知等）不算
    return not last.ebay_message_id.startswith(SIMULATED_PREFIX)


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
                                 order_candidates=_order_candidates(state), buyer_texts=state["snapshot"]["buyer_texts"],
                                 trigger=job.anchor_source_message_id)
        if req.status == InvoiceRequestStatus.DONE:
            return "AUTO_SENT"
        if req.invoice_type == "VAT":
            return "VAT_HUMAN"
        return "INVOICE_READY" if req.order_id and not req.note else "NEEDS_ORDER"
    except Exception:  # noqa: BLE001 — 登记失败不影响草稿落库，人工仍可在会话里手动开票
        log.exception("登记发票申请失败")
        return "FAILED"


def _fresh_buyer_texts(state: AgentState, deps: AgentDeps | None) -> list[str]:
    """本次任务的买家来信里还没开过票的那些。之前没回复的消息会并进这次任务，其中已经触发开票发出的那条（及更早的）
    不再算，免得“谢谢，收到了”又把旧邮箱拿出来再开一张。同一条消息重跑（最后一条就是上次触发的）不截，落库时按已发过处理。"""
    from ..models import InvoiceRequest

    snap = state["snapshot"]
    ids, texts = snap.get("source_message_ids") or [], snap["buyer_texts"]
    conv_id = getattr(getattr(deps, "run", None), "conversation_id", None)
    if not ids or conv_id is None or getattr(deps, "db", None) is None:
        return texts
    row = deps.db.execute(select(InvoiceRequest.trigger_message_id, InvoiceRequest.status)
                          .where(InvoiceRequest.conversation_id == conv_id)).first()
    if row and row[1] == "DONE" and row[0] in ids and row[0] != ids[-1]:
        return texts[ids.index(row[0]) + 1:]
    return texts


def _invoice_texts(state: AgentState, deps: AgentDeps | None = None) -> tuple[list[str], list[str]]:
    """（本次还没开过票的买家来信，其余全部消息：之前的聊天含店铺回复 + 本次里已开过票的那些）。"""
    msgs = state["memory"]["messages"]
    fresh = _fresh_buyer_texts(state, deps)
    used = state["snapshot"]["buyer_texts"][:len(state["snapshot"]["buyer_texts"]) - len(fresh)]
    return fresh, [m["text"] or "" for m in msgs if not m["in_task"]] + list(used)


def _invoice_mode(state: AgentState, deps: AgentDeps, intents: list[str]) -> str | None:
    """发票场景怎么处理：VAT 买家要增值税发票（转人工）/ AUTO_SEND 普通发票，买家给了邮箱就自动开票发出 / None 其他。
    生成前（intents 为空，只看发票相关词）和校验时各算一次，两次结果一致才会自动发（提示词和实际动作对得上）。"""
    buyer, history = _invoice_texts(state)
    scene = any(INVOICE_WORDS.search(t or "") for t in [*buyer, *history])
    if not scene:
        return None
    history_buyer = [m["text"] or "" for m in state["memory"]["messages"] if not m["in_task"] and m["role"] == "BUYER"]
    if suggest_type([*buyer, *history_buyer]) == "VAT":
        return "VAT"
    return "AUTO_SEND" if deps.settings.invoice_auto_send else None


def _video_link_context(state: AgentState, deps: AgentDeps) -> list[str]:
    """买家这次在 eBay 消息里直接发来了视频 / 网盘链接：对得上订单的告诉模型“已收到、已转技术”；
    没写订单号、也对不上是哪笔订单的，回复里请买家补订单号（系统按订单号把视频转到售后群）。"""
    from ..aftersales_mail.message_links import ORDER_RE, links_in
    from ..models import AftersalesCase

    text = "\n".join(state["snapshot"]["buyer_texts"])
    wt, other = links_in(text)
    if not (wt or other):
        return []
    kind = "WeTransfer 视频链接" if wt else "网盘链接"
    m = ORDER_RE.search(text)
    orders = _order_candidates(state)
    order = m.group(0) if m else (orders[0] if len(orders) == 1 else None)
    conv_id = getattr(getattr(deps, "run", None), "conversation_id", None)
    if order is None and conv_id is not None and getattr(deps, "db", None) is not None:
        order = deps.db.scalar(select(AftersalesCase.order_id).where(AftersalesCase.conversation_id == conv_id,
                                                                     AftersalesCase.order_id.is_not(None))
                               .order_by(AftersalesCase.id.desc()).limit(1))
    if order:
        return [f"买家这次发来了{kind}（对应订单 {order}）：系统会自动下载并转给技术同事。回复确认已收到视频，"
                "已转技术同事查看，会尽快回复；不用再要订单号"]
    pick = f"（买家有 {len(orders)} 笔订单：{'、'.join(orders[:5])}，可以列出来让买家选）" if orders else ""
    return [f"买家这次发来了{kind}，但没写订单号，系统对不上是哪笔订单{pick}。回复确认已收到视频，并请买家回复这个视频对应的"
            " eBay 订单号（例如“我的订单号是：12-34567-89012”），收到后马上转给技术同事"]


def _remembered_invoice_email(state: AgentState) -> str | None:
    """买家之前在这个会话里给过的邮箱（最近一个；含本次任务里已开过票的那些来信）。"""
    from ..invoicing.mailer import EMAIL_RE

    texts = [m["text"] or "" for m in state["memory"]["messages"] if not m["in_task"] and m["role"] == "BUYER"]
    found = [m.group(0) for t in [*texts, *state["snapshot"]["buyer_texts"]] for m in EMAIL_RE.finditer(t or "")]
    return found[-1] if found else None


def _asks_invoice_again(state: AgentState, deps: AgentDeps | None, intents: list[str] | None) -> bool:
    """这次是在（再）要发票：本次还没开过票的来信提到发票；校验时还要模型也判成发票意图、不是单纯道谢
    （“谢谢，发票收到了”不再发）。"""
    if any(INVOICE_WORDS.search(t or "") for t in _fresh_buyer_texts(state, deps)):
        return intents is None or ("invoice_vat" in intents and "thanks_closing" not in intents)
    if _waiting_order_choice(state, deps):  # 之前请买家选订单，这次买家回了订单号（来信里可能不再提“发票”）
        return intents is None or set(intents) != {"thanks_closing"}
    return False


def _waiting_order_choice(state: AgentState, deps: AgentDeps | None) -> bool:
    """会话里有发票申请在等买家选订单（给过邮箱、还没定订单、没发出）。"""
    from ..models import InvoiceRequest

    conv_id = getattr(getattr(deps, "run", None), "conversation_id", None)
    if conv_id is None or getattr(deps, "db", None) is None:
        return False
    req = deps.db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv_id))
    return bool(req and req.status == "OPEN" and req.email and not req.order_id and req.invoice_type != "VAT")


def _invoice_context(state: AgentState, deps: AgentDeps) -> list[str]:
    """运行上下文里的发票做法（以此为准，知识库里“先问要哪种发票”的做法已停用）。"""
    lines = ["发票：本店只开普通发票（和 eBay 订单单据一样，列明 eBay 代收代缴的增值税）。买家要发票时只请买家在 eBay 消息里回复接收发票的邮箱，"
             "不用问要哪种发票（以此为准，知识库里“先问要普通发票还是含增值税发票”的做法已停用）。"
             "买家明确要含增值税的发票（公司抬头、税号、单独列明增值税）时，告诉买家已转交同事开具，会通过邮件发送，不承诺具体时间。"
             "发票可以重复开：买家再要发票就照常开具发出。回复里不要说“已经开过/之前已经发过”，也不要说“重新开具/补开/再开一张”，"
             "只说发票已发到邮箱请查收"]
    buyer, history = _invoice_texts(state, deps)
    mode = _invoice_mode(state, deps, [])
    if mode == "AUTO_SEND" and detect_invoice_email(buyer, history, []):
        orders = _order_candidates(state)
        if len(orders) == 1:  # 订单唯一：系统真的会开票发出，回复才能说“已发出”
            sender = deps.settings.invoice_smtp_user
            lines.append("买家这次发来了接收发票的邮箱：系统会自动开具普通发票并马上发到这个邮箱"
                         + (f"（发件邮箱 {sender}）" if sender else "")
                         + "。回复里告诉买家发票已经发到他提供的邮箱，请查收，找不到的话看一下垃圾邮件；不要复述买家的邮箱地址")
        elif orders:
            lines.append(f"买家这次发来了接收发票的邮箱，但买家有 {len(orders)} 笔订单（{'、'.join(orders[:5])}），"
                         "系统不知道要开哪一笔，这次不会开票发出。回复里不要说发票已发出，请买家回复要开发票的订单号"
                         "（可以列出这些订单号让买家选），收到后马上发到他提供的邮箱")
        else:
            lines.append("买家这次发来了接收发票的邮箱，但查不到买家的订单，这次不会开票发出。回复里不要说发票已发出，"
                         "请买家回复要开发票的 eBay 订单号，收到后马上发到他提供的邮箱")
    elif (mode == "AUTO_SEND" and _asks_invoice_again(state, deps, None) and _remembered_invoice_email(state)
          and len(_order_candidates(state)) == 1):
        lines.append("买家这次要发票（或回了之前请他选的订单号）没写邮箱，但之前在这个会话里给过接收邮箱："
                     "如果买家这次是在要发票或回了订单号（不是道谢或确认收到），"
                     "系统会马上开具普通发票发到他之前提供的邮箱，回复里说发票已发到他之前提供的邮箱，请查收，找不到看一下垃圾邮件，"
                     "intents 要包含 invoice_vat；如果只是道谢或确认收到，系统不会再发，正常回复即可，不要说已发出")
    elif mode == "AUTO_SEND" and any(INVOICE_WORDS.search(t or "") for t in buyer):
        lines.append("买家要发票还没给邮箱：请买家回复接收发票的邮箱，收到后系统马上开具并发出（不要说同事开具、不要说无法保证时间）")
    elif mode == "VAT":
        lines.append("这位买家要的是含增值税的发票：系统不开，已转交同事人工开具。回复里告诉买家同事会开具并通过邮件发送；"
                     "买家还没给接收邮箱的，请买家回复邮箱")
    return lines


def _invoice_grounded(db, job: ReplyJob, run, res: dict) -> None:
    """发票已经真实发出：“已发到您的邮箱”这句话的依据就是这张发票（记进草稿依据），不再因“没引用知识库/查询结果”
    （NO_KB_EVIDENCE）卡在审核。其他原因照旧。"""
    from ..models import Invoice

    inv = db.scalar(select(Invoice).where(Invoice.conversation_id == job.conversation_id, Invoice.sent_at.is_not(None))
                    .order_by(Invoice.id.desc()).limit(1))
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == run.run_generation))
    if inv is not None and draft is not None:
        draft.evidence_ids = list(dict.fromkeys([*(draft.evidence_ids or []), f"invoice:{inv.number}"]))
    if "NO_KB_EVIDENCE" not in res["reasons"]:
        return
    res["reasons"] = [r for r in res["reasons"] if r != "NO_KB_EVIDENCE"]
    job.decision_reasons = res["reasons"]
    if draft is not None:
        draft.review_reasons = res["reasons"]
    if not res["reasons"] and res["has_draft"]:
        job.state, res["decision"] = JobState.AUTO_ELIGIBLE, "AUTO_ELIGIBLE"
        if draft is not None:
            draft.decision = "AUTO_ELIGIBLE"


def _force_review(db, job: ReplyJob, run, res: dict, reason: str) -> None:
    """落库后才发现要人工（如普通发票没能自动发出）：改回待审核。"""
    res["reasons"] = [*res["reasons"], reason]
    job.decision_reasons = res["reasons"]
    job.state = JobState.REVIEW_REQUIRED
    res["decision"] = "REVIEW_REQUIRED"
    draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.run_generation == run.run_generation))
    if draft is not None:
        draft.review_reasons, draft.decision = res["reasons"], "REVIEW_REQUIRED"


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
