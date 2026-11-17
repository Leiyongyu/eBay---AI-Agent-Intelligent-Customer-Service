"""系统状态：脱敏的配置就绪情况、模型连通性检查、前端需要的枚举。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import APP_NAME, APP_VERSION
from ..config import get_settings
from ..db import dialect_name, get_db
from ..redis_bus import bus
from ..guards.compliance import RULES_VERSION
from ..guards.risk import GATE_INFO_REASONS, REASON_LABELS
from ..intents import INTENTS, RULE_VERSION
from ..knowledge.vector_index import vector_status
from ..models import KnowledgeChunk, ReplyJob
from ..prompts import prompt_version
from ..agent.nodes import PROMPT_NAMES
from ..providers.chat import get_chat_provider
from ..providers.embeddings import PROBE_TEXT, embedding_ready, probe
from ..knowledge.retriever import estimate_tokens
from .. import usage as usage_log
from ..worker import worker

from ..auth import require_admin  # noqa: E402

from ..models import Aftersales, Role  # noqa: E402

router = APIRouter(prefix="/api", tags=["system"])

REVIEW_REASON_CODES = {
    "FACT_ERROR": "事实错误",
    "POLICY_ERROR": "政策错误",
    "MISSED_QUESTION": "遗漏问题",
    "TONE": "语气",
    "LANGUAGE": "语言或翻译",
    "SHOULD_HANDOFF": "应转人工",
    "COMPLIANCE": "合规问题",
    "OTHER": "其他",
}


@router.get("/health")
def health():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}


@router.get("/system/status")
def status(db: Session = Depends(get_db)):
    s = get_settings()
    agent_reasons = s.agent_not_ready_reasons()
    rag_reasons = s.rag_not_ready_reasons()
    vector = vector_status(refresh=True)
    if s.knowledge_mode == "retrieval":
        rag_reasons += vector.reasons
    emb_ok, emb_missing = embedding_ready()
    db_version = db.execute(text("SELECT version()")).scalar() if dialect_name() in ("postgresql", "mysql") else "SQLite"
    return {
        "database": {"dialect": dialect_name(), "display": s.database_display, "server": str(db_version)[:80]},
        "redis": bus.ping(),
        "app": APP_NAME,
        "version": APP_VERSION,
        "agent_ready": not agent_reasons,
        "agent_status": "READY" if not agent_reasons else "AGENT_NOT_READY",
        "agent_not_ready_reasons": agent_reasons,
        "rag_status": "READY" if not rag_reasons else "RAG_NOT_READY",
        "rag_not_ready_reasons": rag_reasons,
        "vector_index": {"backend": vector.backend, "pgvector_version": vector.version, "reasons": vector.reasons},
        "embedding_configured": emb_ok,
        "embedding_missing": emb_missing,
        "config": s.masked_status(),
        "versions": {
            "prompt_version": prompt_version(*PROMPT_NAMES),
            "rule_version": RULE_VERSION,
            "compliance_rules_version": RULES_VERSION,
            "model_config_version": s.model_config_version(),
        },
        "worker_alive": bool(worker._thread and worker._thread.is_alive()),
        "knowledge_chunks": db.scalar(select(func.count()).select_from(KnowledgeChunk)),
        "jobs_by_state": dict(db.execute(select(ReplyJob.state, func.count()).group_by(ReplyJob.state)).all()),
        "models": _model_status(s),
        "eval_gate": _eval_gate(db),
    }


def _eval_gate(db: Session) -> dict | None:
    """版本门槛（第一层规则评测）：低于门槛时状态页标红。出错不影响状态页。"""
    try:
        from ..evaluation import gate

        return gate.status(db)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _model_status(s) -> dict:
    """模型链和熔断状态：每条链按顺序列出模型，熔断中的标出恢复时间。"""
    from .. import alerts
    from ..providers.model_chain import KINDS, breaker, chain_names

    snap = breaker.snapshot()
    by_name = {x["model"]: x for x in snap}
    chains = {k: [{"model": n, "state": "closed", **by_name.get(n, {})} for n in chain_names(s, k)] for k in KINDS}
    return {"chains": chains, "breaker": snap,
            "breaker_rules": {"failures": s.llm_breaker_failures, "cooldown_minutes": s.llm_breaker_cooldown_minutes,
                              "quota_hours": s.llm_breaker_quota_hours},
            "light_intents": [x for x in (s.llm_light_intents or "").replace("，", ",").split(",") if x.strip()],
            "prompt_cache": bool(s.llm_prompt_cache),
            "alerts": {"enabled": bool(s.alerts_enabled), "recent": alerts.sent[-10:]}}


@router.post("/system/llm-check", dependencies=[Depends(require_admin)])
def llm_check(db: Session = Depends(get_db)):
    """用固定文本调用一次模型，确认 Base URL / 模型 ID / Key 可用（不发送任何买家内容）。"""
    llm = get_chat_provider()
    if llm is None:
        raise HTTPException(503, {"message": "AGENT_NOT_READY", "reasons": get_settings().agent_not_ready_reasons()})
    provider, model = usage_log.chat_model(get_settings())
    started = time.perf_counter()
    try:
        r = llm.ping()
    except Exception as e:
        raise HTTPException(502, {"message": f"模型调用失败：{type(e).__name__}", "detail": str(e)[:500]})
    usage_log.record(db, "check", provider=provider, model=model, usage=r.get("usage"),
                     duration_ms=int((time.perf_counter() - started) * 1000))
    return {"ok": True, **r}


@router.post("/system/embedding-check", dependencies=[Depends(require_admin)])
def embedding_check(db: Session = Depends(get_db)):
    """用固定文本调用一次向量模型，核对实际维度与 EMBEDDING_DIMENSION（第 15.3 节）。"""
    s = get_settings()
    started = time.perf_counter()
    try:
        r = probe()
    except ValueError as e:
        raise HTTPException(422, {"message": str(e)})
    except Exception as e:
        raise HTTPException(502, {"message": f"向量模型调用失败：{type(e).__name__}", "detail": str(e)[:500]})
    usage_log.record(db, "check", provider=s.embedding_provider, model=s.embedding_model,
                     estimated_tokens=estimate_tokens(PROBE_TEXT), duration_ms=int((time.perf_counter() - started) * 1000))
    return {"ok": True, **r}


@router.get("/system/meta")
def meta():
    return {
        "intents": {code: {"name": name, "default_risk": risk} for code, (name, risk) in INTENTS.items()},
        "reason_labels": REASON_LABELS,
        "gate_info_reasons": sorted(GATE_INFO_REASONS),
        "review_reason_codes": REVIEW_REASON_CODES,
        # 下拉框用的站点：店铺开通的站点 + 演示店铺用到的站点
        "marketplaces": sorted(set(get_settings().ebay_marketplace_set) | {"EBAY_US", "EBAY_GB"}),
        "send_delay_seconds": get_settings().send_delay_seconds,  # 发出前可撤回的时长
        "aftersales": {"types": Aftersales.TYPES, "status_labels": Aftersales.STATUS_LABELS,
                       "intent_hint": Aftersales.INTENT_HINT},
        "roles": Role.LABELS,
    }


@router.get("/system/store-credentials", dependencies=[Depends(require_admin)])
def store_credentials(db: Session = Depends(get_db)):
    """店铺凭证脱敏清单：只有店铺名称、平台、站点 ID、是否有秘钥和不可逆指纹。

    不提供明文、密文或任何下载/导出接口；更新只能用 scripts/import_store_credentials.py 整表覆盖导入。
    """
    from ..security.store_credentials import find_by_secret, list_masked

    s = get_settings()
    current = find_by_secret(db, s.ebay_refresh_token) if s.ebay_refresh_token else None
    return {"master_key_configured": bool(s.credentials_master_key),
            "env_refresh_token_store": current.store_name if current else None,
            "stores": list_masked(db)}


@router.get("/system/logistics")
def logistics_status():
    """物流查询配置：谷仓是否已配置、17TRACK 是否就绪及剩余额度（只在打开状态页时调用，查额度不扣费）。"""
    from ..logistics.service import quota_status
    from ..logistics.tickets import worker as ticket_worker

    out = quota_status()
    out["tickets"] = {"running": ticket_worker.running, "sync_time": get_settings().logistics_ticket_sync_time,
                      "last": ticket_worker.last,
                      "last_at": ticket_worker.last_at.isoformat(timespec="seconds") + "Z" if ticket_worker.last_at else None}
    return out


@router.post("/system/logistics/sync-tickets", dependencies=[Depends(require_admin)])
def logistics_sync_tickets():
    """立即同步一次谷仓查件单（只读查询；新回复会起草回复建议，可能要几十秒）。"""
    from ..logistics.tickets import worker as ticket_worker

    if not get_settings().goodcang_ready:
        raise HTTPException(409, "谷仓接口未配置")
    return ticket_worker.run_now()


@router.get("/system/trace/{trace_id}", dependencies=[Depends(require_admin)])
def trace_lookup(trace_id: str, db: Session = Depends(get_db)):
    """按追踪号查一次请求 / 起草 / 发送留下的记录：Agent 运行、工具调用、模型调用、审计、待发消息（日志按同一个号搜）。"""
    from ..models import AgentRun, AuditEvent, LlmUsage, OutboundMessage, ToolCall
    from ..observability import valid_trace_id

    if not valid_trace_id(trace_id):
        raise HTTPException(422, "追踪号格式不对")

    def iso(t):
        return t.isoformat() + "Z" if t else None

    runs = list(db.scalars(select(AgentRun).where(AgentRun.trace_id == trace_id).order_by(AgentRun.id)))
    return {
        "trace_id": trace_id,
        "agent_runs": [{"id": r.id, "job_id": r.job_id, "run_generation": r.run_generation, "thread_id": r.thread_id,
                        "state": r.state, "error": r.error, "started_at": iso(r.started_at), "finished_at": iso(r.finished_at)}
                       for r in runs],
        "tool_calls": [{"id": t.id, "job_id": t.job_id, "tool_name": t.tool_name, "status": t.result_status,
                        "duration_ms": t.duration_ms, "queried_at": iso(t.queried_at)}
                       for t in db.scalars(select(ToolCall).where(ToolCall.trace_id == trace_id).order_by(ToolCall.id).limit(200))],
        "llm_usage": [{"id": u.id, "purpose": u.purpose, "model": u.model, "total_tokens": u.total_tokens,
                       "cached_tokens": u.cached_tokens, "duration_ms": u.duration_ms, "ok": u.ok, "error": u.error,
                       "created_at": iso(u.created_at)}
                      for u in db.scalars(select(LlmUsage).where(LlmUsage.trace_id == trace_id).order_by(LlmUsage.id).limit(200))],
        "audit_events": [{"id": a.id, "actor": a.actor, "action": a.action, "store_id": a.store_id,
                          "conversation_id": a.conversation_id, "detail": a.detail, "created_at": iso(a.created_at)}
                         for a in db.scalars(select(AuditEvent).where(AuditEvent.trace_id == trace_id).order_by(AuditEvent.id).limit(200))],
        "outbound": [{"id": o.id, "conversation_id": o.conversation_id, "source": o.source, "actor": o.actor, "state": o.state,
                      "error": o.error, "created_at": iso(o.created_at), "sent_at": iso(o.sent_at)}
                     for o in db.scalars(select(OutboundMessage).where(OutboundMessage.trace_id == trace_id).order_by(OutboundMessage.id).limit(50))],
    }
