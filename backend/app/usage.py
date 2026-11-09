"""模型调用用量记录（llm_usage 表）与用量统计。

每次调用大模型、视觉模型、向量模型都在调用方当前的数据库会话里记一行，随调用方一起提交：
- 服务商返回了用量就照记（输入/输出/合计）；历史上的 Codex 记录只有合计；向量模型不返回用量时按字数估算并标记 estimated；
- 调用失败也记（ok=False），用于看失败率；
- 不记录任何提示词、买家原文或模型输出，只记数字和归属（店铺、会话、任务）。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import LLM_PROVIDER_LABELS, Settings
from .models import Conversation, Draft, LlmUsage, ReplyJob, Store

PURPOSE_LABELS = {
    "reply": "回复生成",
    "repair": "格式修正",
    "vision": "识图",
    "translate": "翻译",
    "embedding_query": "检索向量",
    "embedding_docs": "知识向量化",
    "check": "连通测试",
}


def translate_model(s: Settings) -> tuple[str | None, str | None]:
    """翻译用的（通道, 模型名）：LLM_TRANSLATE_MODEL，没配用回复模型。"""
    return s.llm_provider, s.translate_model_name


def chat_model(s: Settings) -> tuple[str | None, str | None]:
    """当前回复模型的（通道, 模型名）。"""
    return s.llm_provider, s.llm_chat_model


def record(db: Session, purpose: str, *, provider: str | None, model: str | None, usage: dict | None = None,
           duration_ms: int | None = None, ok: bool = True, error: str | None = None, store_id: str | None = None,
           conversation_id: int | None = None, job_id: int | None = None, estimated_tokens: int | None = None) -> None:
    usage = usage or {}
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and (inp is not None or out is not None):
        total = (inp or 0) + (out or 0)
    estimated = total is None and estimated_tokens is not None
    if estimated:
        inp = total = estimated_tokens
    db.add(LlmUsage(purpose=purpose, provider=provider, model=model, store_id=store_id, conversation_id=conversation_id,
                    job_id=job_id, input_tokens=inp, output_tokens=out, total_tokens=total, estimated=estimated,
                    duration_ms=duration_ms, ok=ok, error=(error or None) and error[:300]))


def usage_delta(after: dict | None, before: dict | None) -> dict:
    """repair 返回的是累计用量（含首次生成），记录时只取这次调用新增的部分。"""
    after, before = after or {}, before or {}
    return {k: v - (before.get(k) or 0) for k, v in after.items()
            if k in ("input_tokens", "output_tokens", "total_tokens") and isinstance(v, (int, float))}


# ------------------------------------------------------------------ 统计
def _local_day(dt: datetime) -> date:
    """按本机时区分日（数据库存 UTC）。"""
    return dt.replace(tzinfo=timezone.utc).astimezone().date()


def _bucket() -> dict:
    return {"calls": 0, "failed": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "duration_ms": 0}


def _add(b: dict, r: LlmUsage) -> None:
    b["calls"] += 1
    b["failed"] += 0 if r.ok else 1
    b["total_tokens"] += r.total_tokens or 0
    b["input_tokens"] += r.input_tokens or 0
    b["output_tokens"] += r.output_tokens or 0
    b["duration_ms"] += r.duration_ms or 0


def _finish(b: dict) -> dict:
    b["avg_tokens"] = round(b["total_tokens"] / b["calls"]) if b["calls"] else 0
    b["avg_seconds"] = round(b.pop("duration_ms") / b["calls"] / 1000, 1) if b["calls"] else 0
    return b


def summary(db: Session, start: date, end: date, store_id: str | None = None) -> dict:
    """[start, end] 按本机日期闭区间统计。"""
    # 本机日期边界换算成 UTC 查询，前后各放宽一天再按本机日期精确过滤
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time())
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time())
    q = (select(LlmUsage).where(LlmUsage.created_at >= lo, LlmUsage.created_at < hi)
         .order_by(LlmUsage.created_at, LlmUsage.id))
    if store_id:
        q = q.where(LlmUsage.store_id == store_id)
    rows = [r for r in db.scalars(q) if start <= _local_day(r.created_at) <= end]

    totals = _bucket()
    estimated = 0
    days: dict[date, dict] = {start + timedelta(days=i): {**_bucket(), "by_purpose": defaultdict(int)}
                              for i in range((end - start).days + 1)}
    by_purpose: dict[str, dict] = defaultdict(_bucket)
    by_model: dict[tuple, dict] = defaultdict(_bucket)
    by_store: dict[str | None, dict] = defaultdict(_bucket)
    by_conv: dict[int, dict] = defaultdict(_bucket)
    for r in rows:
        _add(totals, r)
        estimated += (r.total_tokens or 0) if r.estimated else 0
        day = days[_local_day(r.created_at)]
        _add(day, r)
        day["by_purpose"][r.purpose] += r.total_tokens or 0
        _add(by_purpose[r.purpose], r)
        _add(by_model[(r.provider, r.model)], r)
        _add(by_store[r.store_id], r)
        if r.conversation_id:
            _add(by_conv[r.conversation_id], r)

    # 草稿数：同期 AI 生成的草稿，用来算“每条草稿平均 token”
    dq = select(Draft.created_at).where(Draft.author == "agent", Draft.created_at >= lo, Draft.created_at < hi)
    if store_id:
        dq = dq.join(ReplyJob, ReplyJob.id == Draft.job_id).where(ReplyJob.store_id == store_id)
    drafts = sum(1 for (t,) in db.execute(dq) if start <= _local_day(t) <= end)
    draft_tokens = by_purpose["reply"]["total_tokens"] + by_purpose["repair"]["total_tokens"] if rows else 0

    stores = {s.id: s.name for s in db.scalars(select(Store))}
    top_ids = sorted(by_conv, key=lambda c: by_conv[c]["total_tokens"], reverse=True)[:15]
    convs = {c.id: c for c in db.scalars(select(Conversation).where(Conversation.id.in_(top_ids)))} if top_ids else {}

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": len(days)},
        "totals": {**_finish(totals), "estimated_tokens": estimated, "drafts": drafts,
                   "tokens_per_draft": round(draft_tokens / drafts) if drafts else None},
        "by_day": [{"date": d.isoformat(), **_finish({k: v for k, v in b.items() if k != "by_purpose"}),
                    "by_purpose": dict(b["by_purpose"])} for d, b in sorted(days.items())],
        "by_purpose": sorted(({"purpose": p, "label": PURPOSE_LABELS.get(p, p), **_finish(b)}
                              for p, b in by_purpose.items() if b["calls"]),
                             key=lambda x: x["total_tokens"], reverse=True),
        "by_model": sorted(({"provider": p, "provider_label": LLM_PROVIDER_LABELS.get(p or "", p or "未知"),
                             "model": m or "未知", **_finish(b)} for (p, m), b in by_model.items()),
                           key=lambda x: x["total_tokens"], reverse=True),
        "by_store": sorted(({"store_id": s, "name": stores.get(s, s) if s else "（不属于店铺：翻译、测试等）",
                             **_finish(b)} for s, b in by_store.items()),
                           key=lambda x: x["total_tokens"], reverse=True),
        "top_conversations": [{"conversation_id": c, "store_id": convs[c].store_id if c in convs else None,
                               "store_name": stores.get(convs[c].store_id) if c in convs else None,
                               "title": convs[c].conversation_title if c in convs else None,
                               "buyer_key": convs[c].buyer_key if c in convs else None, **_finish(by_conv[c])}
                              for c in top_ids],
        "recent": [{"id": r.id, "created_at": r.created_at.isoformat() + "Z", "purpose": r.purpose,
                    "purpose_label": PURPOSE_LABELS.get(r.purpose, r.purpose), "provider": r.provider,
                    "model": r.model, "store_id": r.store_id, "store_name": stores.get(r.store_id or ""),
                    "conversation_id": r.conversation_id, "job_id": r.job_id, "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens, "total_tokens": r.total_tokens, "estimated": r.estimated,
                    "duration_ms": r.duration_ms, "ok": r.ok, "error": r.error}
                   for r in reversed(rows[-100:])],
        "purposes": PURPOSE_LABELS,
    }
