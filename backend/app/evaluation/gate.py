"""版本门槛：提示词 / 规则 / 合规 / 知识库任一版本变化 → 跑第一层规则评测，低于门槛（或比基线掉太多）
就在系统状态页标红、记审计 EVAL_GATE_FAILED。不阻断服务（只是提醒）；CI 里 scripts/eval_rules.py --gate 返回非 0 阻断合并。

启动时检查（server lifespan 调 startup_check）：版本和最近一次规则评测不同就在后台起一个子进程跑
scripts/eval_rules.py --trigger startup——评测在独立的 SQLite 库里回放，不往业务库写会话 / 任务，只把结果写回 eval_runs。
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import AuditEvent, EvalRun, KnowledgeChunk
from .golden import ROOT

log = logging.getLogger(__name__)


def knowledge_version(db: Session) -> str:
    """生效知识的指纹：所有已发布条款的（店铺, 条款, 版本）排序后取哈希，条款增删改都会变。"""
    rows = db.execute(select(KnowledgeChunk.store_id, KnowledgeChunk.clause_id, KnowledgeChunk.version)
                      .where(KnowledgeChunk.review_state == "PUBLISHED")).all()
    h = hashlib.sha256("\n".join(sorted(f"{a}|{b}|{c}" for a, b, c in rows)).encode()).hexdigest()[:12]
    return f"{len(rows)}:{h}"


def current_versions(db: Session) -> dict:
    from ..agent.nodes import PROMPT_NAMES
    from ..guards.compliance import RULES_VERSION as COMPLIANCE_VERSION
    from ..intents import RULE_VERSION
    from ..prompts import prompt_version

    return {"prompt": prompt_version(*PROMPT_NAMES), "rules": RULE_VERSION, "compliance": COMPLIANCE_VERSION,
            "knowledge": knowledge_version(db)}


def last_run(db: Session, kind: str = "rules") -> EvalRun | None:
    return db.scalar(select(EvalRun).where(EvalRun.kind == kind, EvalRun.error.is_(None))
                     .order_by(EvalRun.id.desc()).limit(1))


def baseline(db: Session, kind: str = "rules") -> EvalRun | None:
    return db.scalar(select(EvalRun).where(EvalRun.kind == kind, EvalRun.is_baseline.is_(True))
                     .order_by(EvalRun.id.desc()).limit(1))


def changed(db: Session) -> list[str]:
    """和最近一次规则评测相比变了的版本（空 = 没变；从没评过 = 全部）。"""
    now = current_versions(db)
    last = last_run(db)
    if last is None:
        return list(now)
    return [k for k, v in now.items() if (last.versions or {}).get(k) != v]


def judge(pass_rate: float | None, base: float | None) -> bool:
    """是否不过：低于门槛，或比基线掉了超过 EVAL_MAX_DROP。"""
    s = get_settings()
    if pass_rate is None:
        return True
    return pass_rate < s.eval_min_pass_rate or (base is not None and pass_rate < base - s.eval_max_drop)


def record(db: Session, summary: dict, *, kind: str = "rules", trigger: str = "manual", dataset: str = "golden",
           versions: dict | None = None, actor: str = "system", error: str | None = None,
           set_baseline: bool = False) -> EvalRun:
    """记一次评测；不过时记审计 EVAL_GATE_FAILED。"""
    base = baseline(db, kind)
    rate = summary.get("pass_rate") if summary else None
    below = bool(error) or judge(rate, base.pass_rate if base else None)
    run = EvalRun(kind=kind, trigger=trigger, dataset=dataset, versions=versions or current_versions(db),
                  cases=summary.get("cases", 0) if summary else 0, judged=summary.get("judged", 0) if summary else 0,
                  pass_rate=rate, summary=summary, threshold=get_settings().eval_min_pass_rate,
                  baseline_pass_rate=base.pass_rate if base else None, below=below, is_baseline=set_baseline and not below,
                  error=error, created_by=actor[:64], created_at=utcnow())
    db.add(run)
    db.flush()
    if below:
        db.add(AuditEvent(actor=actor[:64], action="EVAL_GATE_FAILED",
                          detail={"eval_run_id": run.id, "kind": kind, "trigger": trigger, "pass_rate": rate,
                                  "threshold": run.threshold, "baseline": run.baseline_pass_rate, "error": error,
                                  "versions": run.versions}))
    return run


def mark_baseline(db: Session, run_id: int, actor: str) -> EvalRun:
    run = db.get(EvalRun, run_id)
    if run is None or run.error or run.below:
        raise ValueError("只能把一次没出错、没低于门槛的评测标为基线")
    for r in db.scalars(select(EvalRun).where(EvalRun.kind == run.kind, EvalRun.is_baseline.is_(True))):
        r.is_baseline = False
    run.is_baseline = True
    db.add(AuditEvent(actor=actor[:64], action="EVAL_BASELINE_SET", detail={"eval_run_id": run.id, "pass_rate": run.pass_rate}))
    return run


def status(db: Session) -> dict:
    """系统状态页用：最近一次规则评测、基线、门槛、版本有没有变、最近一次 LLM 评审。"""
    s = get_settings()
    last, base, jr = last_run(db), baseline(db), last_run(db, "judge")
    chg = changed(db)
    return {
        "threshold": s.eval_min_pass_rate, "max_drop": s.eval_max_drop, "on_startup": s.eval_gate_on_startup,
        "versions": current_versions(db), "changed_since_last": chg,
        "last": _run_dict(last), "baseline": _run_dict(base), "judge_last": _run_dict(jr),
        "judge_enabled": s.eval_judge_enabled,
        "red": bool(last and last.below) or last is None,
        "runs_total": db.scalar(select(func.count()).select_from(EvalRun)),
    }


def _run_dict(r: EvalRun | None) -> dict | None:
    if r is None:
        return None
    return {"id": r.id, "kind": r.kind, "trigger": r.trigger, "dataset": r.dataset, "cases": r.cases, "judged": r.judged,
            "pass_rate": r.pass_rate, "below": r.below, "threshold": r.threshold, "baseline_pass_rate": r.baseline_pass_rate,
            "is_baseline": r.is_baseline, "error": r.error, "versions": r.versions,
            "created_at": r.created_at.isoformat(timespec="seconds") + "Z" if r.created_at else None,
            "by_stratum": (r.summary or {}).get("by_stratum"), "by_check": (r.summary or {}).get("by_check")}


def startup_check() -> bool:
    """启动时：版本变了（或从没评过）就在后台子进程跑一次第一层规则评测。返回是否启动了子进程。"""
    from ..db import session_scope

    if not get_settings().eval_gate_on_startup:
        return False
    try:
        with session_scope() as db:
            chg = changed(db)
    except Exception as e:  # noqa: BLE001  评测门槛出问题不能影响启动
        log.warning("评测门槛检查失败：%s", e)
        return False
    if not chg:
        return False
    log.info("提示词 / 规则 / 合规 / 知识库版本有变化（%s），后台跑第一层规则评测", "、".join(chg))
    cmd = [sys.executable, str(ROOT / "scripts" / "eval_rules.py"), "--trigger", "startup", "--quiet"]
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)  # noqa: S603
    return True
