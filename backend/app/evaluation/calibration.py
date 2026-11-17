"""置信度校准（第二批 A6）：模型自报的意图把握度（intent_confidence：high / medium / low）对照审核员实际改动的比例，
看“高把握度”是不是真的改得少；按意图给“自动发送候选”做依据（高把握度样本够多、几乎不被改的意图）。"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy.orm import Session

from ..db import utcnow
from .feedback import EDIT_MIN, collect

BUCKETS = ("high", "medium", "low", "none")
HEAVY = 0.3
CANDIDATE_MIN_N = 20       # 高把握度至少多少条才能当自动发送候选
CANDIDATE_MAX_EDITED = 0.1  # 被改比例不超过多少


def table(db: Session, days: int = 30) -> dict:
    rows = collect(db, utcnow() - timedelta(days=days))

    def stats(rs: list[dict]) -> dict:
        n = len(rs)
        return {"n": n,
                "edited_rate": round(sum(r["edit_ratio"] >= EDIT_MIN for r in rs) / n, 3) if n else None,
                "heavy_rate": round(sum(r["edit_ratio"] >= HEAVY for r in rs) / n, 3) if n else None,
                "unchanged_rate": round(sum(r["edit_ratio"] == 0 for r in rs) / n, 3) if n else None,
                "avg_edit_ratio": round(sum(r["edit_ratio"] for r in rs) / n, 3) if n else None}

    by_bucket: dict[str, list] = defaultdict(list)
    by_intent: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        b = r["intent_confidence"] if r["intent_confidence"] in BUCKETS else "none"
        by_bucket[b].append(r)
        by_intent[(r["intent"], b)].append(r)
    intents = sorted({i for i, _ in by_intent})
    per_intent = {i: {b: stats(by_intent[(i, b)]) for b in BUCKETS if by_intent.get((i, b))} for i in intents}
    candidates = [i for i in intents if (h := per_intent[i].get("high")) and h["n"] >= CANDIDATE_MIN_N
                  and h["edited_rate"] is not None and h["edited_rate"] <= CANDIDATE_MAX_EDITED]
    hi, lo = stats(by_bucket["high"]), stats(by_bucket["low"])
    return {"days": days, "reviewed": len(rows), "edit_threshold": EDIT_MIN, "heavy_threshold": HEAVY,
            "buckets": {b: stats(by_bucket[b]) for b in BUCKETS},
            "monotonic": None if not (hi["n"] and lo["n"]) else hi["edited_rate"] <= lo["edited_rate"],
            "per_intent": per_intent,
            "auto_send_candidates": candidates,
            "rule": f"高把握度至少 {CANDIDATE_MIN_N} 条、被改比例 ≤ {int(CANDIDATE_MAX_EDITED * 100)}% 的意图列为自动发送候选（仍需人工决定是否放开）"}
