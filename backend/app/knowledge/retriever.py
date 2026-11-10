"""公司知识装载：全量注入模式（第 12.3、15.5 节）与检索模式（第 15.4 节，retrieve）。

过滤规则：
  review_state=PUBLISHED；effective_at ≤ 查询日；expires_at 为空或 ≥ 查询日；
  store_id ∈ {当前店铺, ALL}；marketplace_id ∈ {当前站点, ALL}；对外回复通道只取 PUBLIC_REPLY。
站点为 UNKNOWN 时只取 marketplace_id=ALL 的条款，不套用站点专属政策（第 11.3 节）。

取数（2026-11-10 提速）：先在数据库里按同样的条件过滤（candidate_query，走 PUBLISHED 部分索引），再用 exclusion_reason
逐条复核，结果与“全表读出再在应用侧过滤”完全一样。原来每次起草都把 8.3 万行（大多是 RETIRED / WITHDRAWN 的旧版本）
整表读进内存，单这一步约 1.8 秒。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import usage as usage_log
from ..config import get_settings
from ..models import KnowledgeChunk

_CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


def estimate_tokens(text: str) -> int:
    """粗略估算：中日韩字符按 1 token，其余按 4 字符 1 token。调用前计量用，不替代服务商的真实用量。"""
    cjk = len(_CJK.findall(text))
    return cjk + max(0, len(text) - cjk) // 4 + 1


def exclusion_reason(c: KnowledgeChunk, store_id: str, marketplace_id: str, on: date, audience: str) -> str | None:
    day = on.isoformat()
    if c.review_state != "PUBLISHED":
        return f"未发布（{c.review_state}）"
    if c.effective_at > day:
        return f"尚未生效（{c.effective_at} 起）"
    if c.expires_at and c.expires_at < day:
        return f"已失效（{c.expires_at}）"
    if c.store_id not in (store_id, "ALL"):
        return f"其他店铺（{c.store_id}）"
    if marketplace_id == "UNKNOWN":
        if c.marketplace_id != "ALL":
            return "站点未知，不套用站点专属条款"
    elif c.marketplace_id not in (marketplace_id, "ALL"):
        return f"其他站点（{c.marketplace_id}）"
    if c.audience != audience:
        return "仅内部，不用于回复买家" if c.audience == "INTERNAL_SOP" else f"可见级别 {c.audience}"
    return None


def candidate_query(store_id: str, marketplace_id: str, on: date, audience: str):
    """数据库侧过滤：条件与 exclusion_reason 一一对应（只会多取、不会漏取，取回后仍逐条复核）。"""
    day = on.isoformat()
    sites = ["ALL"] if marketplace_id == "UNKNOWN" else [marketplace_id, "ALL"]
    return select(KnowledgeChunk).where(
        KnowledgeChunk.review_state == "PUBLISHED", KnowledgeChunk.store_id.in_([store_id, "ALL"]),
        KnowledgeChunk.marketplace_id.in_(sites), KnowledgeChunk.audience == audience,
        KnowledgeChunk.effective_at <= day,
        or_(KnowledgeChunk.expires_at.is_(None), KnowledgeChunk.expires_at == "", KnowledgeChunk.expires_at >= day))


def eligible_chunks(db: Session, store_id: str, marketplace_id: str, on: date, audience: str) -> list[KnowledgeChunk]:
    rows = db.scalars(candidate_query(store_id, marketplace_id, on, audience)).all()
    return [c for c in rows if exclusion_reason(c, store_id, marketplace_id, on, audience) is None]


@dataclass
class KnowledgeContext:
    clauses: list[KnowledgeChunk] = field(default_factory=list)
    tokens: int = 0
    over_budget: bool = False

    @property
    def versions(self) -> list[str]:
        return [evidence_id(c) for c in self.clauses]


def evidence_id(c: KnowledgeChunk) -> str:
    return f"kb:{c.clause_id}@{c.version}"


def load_full_context(db: Session, store_id: str, marketplace_id: str, on: date, budget_tokens: int) -> KnowledgeContext:
    clauses = eligible_chunks(db, store_id, marketplace_id, on, "PUBLIC_REPLY")
    # 优先级数值越小越优先；同优先级店铺专属在前
    clauses.sort(key=lambda c: (c.priority, c.store_id == "ALL", c.clause_id))
    tokens = sum(estimate_tokens(c.title + c.text) + 12 for c in clauses)
    return KnowledgeContext(clauses=clauses, tokens=tokens, over_budget=tokens > budget_tokens)


RRF_K = 60  # 倒数排名融合常数（第 15.4 节）
_SKU_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_.-]{2,39}")


@dataclass
class RetrievalContext(KnowledgeContext):
    """检索模式结果：clauses 已按融合分数排序并按预算截断；hits 是每条候选的打分明细，写进 agent_steps 便于排查。"""
    query: str = ""
    query_tokens: list[str] = field(default_factory=list)
    candidates: int = 0
    hits: list[dict] = field(default_factory=list)
    query_vector_cached: bool = False


def retrieve(db: Session, store_id: str, marketplace_id: str, on: date, query: str, *, top_k: int,
             candidates: int, budget_tokens: int, audience: str = "PUBLIC_REPLY") -> RetrievalContext:
    """混合检索：先按适用范围过滤，再分别做向量、全文、SKU 精确三路召回，用 RRF 融合。

    未就绪（无 pgvector、向量模型未配置）抛 RagNotReady；适用条款里还有没生成向量的也抛，
    避免“看起来检索了其实漏了一批条款”。
    """
    from . import vector_index as vi
    from .tokenize import tokens

    vi.require_ready()
    eligible = eligible_chunks(db, store_id, marketplace_id, on, audience)
    q_tokens = tokens(query)
    ctx = RetrievalContext(query=query, query_tokens=q_tokens, candidates=len(eligible))
    if not eligible or not query.strip():
        return ctx

    by_hash: dict[str, list[KnowledgeChunk]] = {}
    for c in eligible:
        by_hash.setdefault(vi.text_hash(c.text), []).append(c)
    missing = [h for h in by_hash if h not in vi.existing_hashes(db, vi.current_profile(db).id, list(by_hash))]
    if missing:
        raise vi.RagNotReady([f"有 {sum(len(by_hash[h]) for h in missing)} 条适用条款还没生成向量，请在知识库页重建索引"])
    if vi.fill_search_text(eligible):
        db.flush()

    ranks: dict[str, dict] = {c.chunk_id: {} for c in eligible}
    started = time.perf_counter()
    qvec, cached = vi.embed_query_cached(query)
    ctx.query_vector_cached = cached
    if not cached:  # 同一段问题（如重新生成）用缓存的查询向量，不再调用、不记费用
        s = get_settings()
        usage_log.record(db, "embedding_query", provider=s.embedding_provider, model=s.embedding_model, store_id=store_id,
                         estimated_tokens=estimate_tokens(query), duration_ms=int((time.perf_counter() - started) * 1000))
    for rank, (h, dist) in enumerate(vi.vector_search(db, qvec, list(by_hash), candidates), 1):
        for c in by_hash[h]:
            ranks[c.chunk_id].update(vector_rank=rank, distance=round(dist, 4))
    for rank, (cid, score) in enumerate(vi.keyword_search(db, eligible, q_tokens, candidates), 1):
        ranks[cid].update(keyword_rank=rank, keyword_score=round(score, 4))
    query_skus = set(_SKU_TOKEN.findall(query.upper()))
    sku_hits = [c for c in eligible if query_skus & {str(s).upper() for s in (c.sku or [])}]
    for rank, c in enumerate(sorted(sku_hits, key=lambda c: (c.priority, c.chunk_id)), 1):
        ranks[c.chunk_id].update(sku_rank=rank)

    def rrf(r: dict) -> float:
        return sum(1 / (RRF_K + r[k]) for k in ("vector_rank", "keyword_rank", "sku_rank") if k in r)

    chunk_of = {c.chunk_id: c for c in eligible}
    scored = sorted(((rrf(r), cid) for cid, r in ranks.items() if r), key=lambda x: (-x[0], chunk_of[x[1]].priority, x[1]))
    for score, cid in scored[:top_k]:
        c = chunk_of[cid]
        cost = estimate_tokens(c.title + c.text) + 12
        hit = {"chunk_id": cid, "clause_id": c.clause_id, "title": c.title, "rrf": round(score, 5), **ranks[cid]}
        if ctx.tokens + cost > budget_tokens:
            ctx.over_budget = True
            hit["dropped"] = "超出检索预算"
        else:
            ctx.clauses.append(c)
            ctx.tokens += cost
        ctx.hits.append(hit)
    return ctx


def render_clause(c: KnowledgeChunk) -> str:
    scope = f"store={c.store_id}; site={c.marketplace_id}; topic={c.topic}; priority={c.priority}"
    return f'<clause id="{evidence_id(c)}" {scope}>\n{c.text}\n</clause>'
