"""起草前的模型策略：分级路由（轻量档 / 主力档）、预算护栏、语义缓存。全部按配置开，默认不改变现有行为。

分级路由 route_tier：
  只有同时满足才走轻量档（LLM_LIGHT_MODELS 链），否则主力档（LLM_CHAT_MODELS 链）：
  - 配了 LLM_LIGHT_MODELS 和 LLM_LIGHT_INTENTS；
  - 这个会话上一份草稿判断出的意图都在 LLM_LIGHT_INTENTS 里（问候、物流进度、模板类问题）；
  - 没有转售后、没有开着的售后 / 纠纷、这次来信不带附件、上一份草稿风险不高于 L1。
  起草前还不知道这次的意图，所以只看“上一轮已经判断出的意图 + 会话的售后状态”，拿不准一律主力档。

预算护栏 budget_status：
  按店铺 / 按天统计回复起草（reply / repair）的 token；LLM_DAILY_TOKEN_BUDGET（个别店铺 LLM_STORE_TOKEN_BUDGETS）。
  超过预算发告警（每店每天一次）；超过预算 × LLM_BUDGET_HARD_RATIO 不再起草（转人工，只用模板 / 人工回复）。

语义缓存 cache_lookup / cache_store（REPLY_CACHE_ENABLED，默认关）：
  - 存：审核通过、单一意图且在 REPLY_CACHE_INTENTS 里、证据不涉及订单 / 包裹的草稿，记问题向量、知识版本键、结构化输出；
  - 取：同店铺、同知识版本键、问题向量相似度 ≥ REPLY_CACHE_THRESHOLD、REPLY_CACHE_DAYS 天内的，直接作为模型输出
    （再走一遍校验、合规和闸门，草稿标“来自缓存”，照样要人工审核）；命中次数进用量统计。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import Conversation, Draft, LlmUsage, ReplyCacheEntry, ReplyJob, Store

log = logging.getLogger(__name__)
LOW_RISK = ("L0", "L1")
ORDER_EVIDENCE = ("order", "package", "tracking", "shipment", "parcel")


def _set(v: str | None) -> set[str]:
    return {x.strip() for x in (v or "").replace("，", ",").split(",") if x.strip()}


# ------------------------------------------------------------------ 分级路由
def route_tier(db: Session, settings: Settings, conversation_id: int, *, open_flags: set | None = None,
               has_attachments: bool = False) -> tuple[str, str]:
    """(档位, 原因)。档位 light / main。"""
    light = _set(settings.llm_light_intents)
    if not (settings.llm_light_models or "").strip() or not light:
        return "main", "没有配置轻量档（默认全部主力档）"
    if has_attachments:
        return "main", "来信带附件"
    if open_flags:
        return "main", "会话有开着的售后 / 物流问题"
    conv = db.get(Conversation, conversation_id)
    if conv is not None and conv.aftersales_status in ("PENDING", "IN_PROGRESS"):
        return "main", "会话已转售后"
    last = db.scalar(select(Draft).join(ReplyJob, ReplyJob.id == Draft.job_id)
                     .where(ReplyJob.conversation_id == conversation_id, Draft.author == "agent")
                     .order_by(Draft.id.desc()).limit(1))
    if last is None or not last.intents:
        return "main", "还没有判断过意图"
    intents = set(last.intents)
    if not intents <= light:
        return "main", f"上一轮意图 {','.join(sorted(intents - light))} 不在轻量档"
    if (last.risk_level or "L3") not in LOW_RISK:
        return "main", f"上一轮风险 {last.risk_level}"
    return "light", f"上一轮意图 {','.join(sorted(intents))}，低风险"


# ------------------------------------------------------------------ 预算护栏
def store_budget(settings: Settings, store: Store | None) -> int:
    """这个店铺每天的 token 预算；0 = 不限。"""
    if settings.llm_store_token_budgets and store is not None:
        try:
            m = json.loads(settings.llm_store_token_budgets)
            for k in (store.id, store.name):
                if k in m:
                    return int(m[k])
        except (json.JSONDecodeError, TypeError, ValueError):
            log.warning("LLM_STORE_TOKEN_BUDGETS 不是合法 JSON")
    return int(settings.llm_daily_token_budget or 0)


def day_start(now: datetime | None = None) -> datetime:
    n = now or utcnow()
    return datetime(n.year, n.month, n.day)


def used_today(db: Session, store_id: str, now: datetime | None = None) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(LlmUsage.total_tokens), 0)).where(
        LlmUsage.store_id == store_id, LlmUsage.purpose.in_(("reply", "repair")),
        LlmUsage.created_at >= day_start(now))) or 0)


def budget_status(db: Session, settings: Settings, store_id: str, now: datetime | None = None) -> dict:
    """{budget, used, ratio, state: ok / over（超预算，告警）/ blocked（超过硬阈值，不再起草）}。budget=0 时 state=ok。"""
    store = db.get(Store, store_id)
    budget = store_budget(settings, store)
    used = used_today(db, store_id, now) if budget > 0 else 0
    ratio = (used / budget) if budget > 0 else 0.0
    hard = max(1.0, float(settings.llm_budget_hard_ratio or 1.0))
    state = "ok" if budget <= 0 or ratio < 1 else ("blocked" if ratio >= hard else "over")
    return {"store_id": store_id, "store_name": store.name if store else store_id, "budget": budget, "used": used,
            "ratio": round(ratio, 3), "hard_ratio": hard, "state": state}


def budget_alert(st: dict) -> None:
    if st["state"] == "ok":
        return
    from .. import alerts

    day = day_start().date().isoformat()
    text = (f"店铺 {st['store_name']} 今天已用 {st['used']:,} token，预算 {st['budget']:,}（{st['ratio']:.0%}）。"
            + ("已超过降级阈值：之后的来信不再用模型起草，转人工处理。" if st["state"] == "blocked" else "超过预算，继续超到降级阈值后会停止起草。"))
    alerts.notify("budget", "模型预算超标" if st["state"] == "over" else "模型预算超标：已降级", text,
                  level="warning" if st["state"] == "over" else "error", key=f"budget:{st['store_id']}:{day}:{st['state']}")


# ------------------------------------------------------------------ 语义缓存
def knowledge_key(versions: list | None) -> str:
    return hashlib.sha256(json.dumps(sorted(versions or []), ensure_ascii=False).encode()).hexdigest()[:32]


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cache_lookup(db: Session, settings: Settings, store_id: str, question: str, versions: list | None,
                 embed) -> tuple[ReplyCacheEntry | None, float]:
    """找一份可以复用的草稿：返回 (条目, 相似度)。embed(text) -> 向量（用检索时已算过的那份，不额外调模型）。"""
    if not settings.reply_cache_enabled or not question.strip():
        return None, 0.0
    key = knowledge_key(versions)
    since = utcnow() - timedelta(days=max(1, settings.reply_cache_days))
    rows = list(db.scalars(select(ReplyCacheEntry).where(
        ReplyCacheEntry.store_id == store_id, ReplyCacheEntry.knowledge_key == key, ReplyCacheEntry.created_at >= since,
        ReplyCacheEntry.intent.in_(_set(settings.reply_cache_intents))).order_by(ReplyCacheEntry.id.desc()).limit(200)))
    if not rows:
        return None, 0.0
    try:
        vec = embed(question)
    except Exception as e:  # noqa: BLE001  向量算不出：不用缓存，照常起草
        log.warning("语义缓存：问题向量算不出（%s），照常起草", e)
        return None, 0.0
    best, score = None, 0.0
    for r in rows:
        sc = _cos(vec, r.embedding or [])
        if sc > score:
            best, score = r, sc
    if best is None or score < settings.reply_cache_threshold:
        return None, score
    best.hits = (best.hits or 0) + 1
    best.last_hit_at = utcnow()
    return best, score


def cacheable(settings: Settings, draft: Draft) -> str | None:
    """这份草稿能不能进缓存；能返回意图，不能返回 None。"""
    intents = list(draft.intents or [])
    if len(intents) != 1 or intents[0] not in _set(settings.reply_cache_intents):
        return None
    if (draft.risk_level or "L3") not in LOW_RISK:
        return None
    if any(any(w in str(e).lower() for w in ORDER_EVIDENCE) for e in (draft.evidence_ids or [])):
        return None  # 依赖订单 / 包裹的回答不缓存
    return intents[0]


def cache_store(db: Session, settings: Settings, draft: Draft, job: ReplyJob, final_text: str, question: str, embed) -> bool:
    """审核通过后存进缓存（只在 REPLY_CACHE_ENABLED 时）。返回是否存了。"""
    if not settings.reply_cache_enabled or not question.strip():
        return False
    intent = cacheable(settings, draft)
    if intent is None:
        return False
    try:
        vec = embed(question)
    except Exception as e:  # noqa: BLE001
        log.warning("语义缓存：存草稿时问题向量算不出（%s）", e)
        return False
    # 按 ReplyDraftOutput 存（复用时直接 model_validate）：旧草稿的 claims 引用的是旧会话的证据 ID，不带过去
    output = {"intent": intent, "intents": [intent], "language": draft.language or "en", "action": "answer",
              "needs_human": False, "claims": [], "missing_facts": [], "conflicts": [],
              "reasoning_zh": f"来自缓存：复用审核通过的草稿 #{draft.id}（同类通用问题）"[:600],
              "reply_text": final_text[:3000], "reply_zh": (draft.text_zh or "")[:3000], "reason_code": "FROM_CACHE"}
    db.add(ReplyCacheEntry(store_id=job.store_id, intent=intent, language=draft.language,
                           knowledge_key=knowledge_key(draft.knowledge_versions), question=question[:4000],
                           embedding=list(vec), output=output, draft_id=draft.id, created_at=utcnow()))
    return True


def cache_stats(db: Session, since: datetime, store_ids: list[str] | None = None) -> dict:
    q = select(func.count(ReplyCacheEntry.id), func.coalesce(func.sum(ReplyCacheEntry.hits), 0))
    if store_ids is not None:
        q = q.where(ReplyCacheEntry.store_id.in_(store_ids))
    entries, hits = db.execute(q).one()
    return {"entries": int(entries or 0), "hits": int(hits or 0)}
