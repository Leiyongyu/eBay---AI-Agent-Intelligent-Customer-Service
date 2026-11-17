"""评测与质量闭环（第二批 A）：版本门槛状态、评测记录、基线；人工修改回流的改进建议、知识缺口；置信度校准。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_admin
from ..db import get_db
from ..evaluation import gate
from ..models import EvalRun

router = APIRouter(prefix="/internal/eval", tags=["evaluation"])


@router.get("/gate")
def gate_status(db: Session = Depends(get_db)):
    """版本门槛：最近一次第一层规则评测、基线、门槛、版本有没有变、最近一次 LLM 评审。"""
    return gate.status(db)


@router.get("/runs")
def list_runs(kind: str | None = None, limit: int = 60, db: Session = Depends(get_db)):
    """评测记录（新的在前）：上线效果页画趋势用。"""
    q = select(EvalRun).order_by(EvalRun.id.desc()).limit(max(1, min(limit, 500)))
    if kind:
        q = q.where(EvalRun.kind == kind)
    return [gate._run_dict(r) for r in db.scalars(q)]


@router.post("/runs/{run_id}/baseline", dependencies=[Depends(require_admin)])
def set_baseline(run_id: int, request: Request, db: Session = Depends(get_db)):
    """把一次评测标为基线：之后的评测比它低出 EVAL_MAX_DROP 也算不过。"""
    try:
        run = gate.mark_baseline(db, run_id, current_user(request).display_name)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    db.commit()
    return gate._run_dict(run)


# ------------------------------------------------------------------ 人工修改回流（A5）
@router.get("/edits")
def edit_stats(days: int = 7, db: Session = Depends(get_db)):
    """近 days 天审核员对 AI 草稿的修改：按修改类型、按意图。"""
    from datetime import timedelta

    from ..db import utcnow
    from ..evaluation import feedback

    return {"days": days, **feedback.edit_stats(feedback.collect(db, utcnow() - timedelta(days=max(1, min(days, 180)))))}


@router.get("/suggestions")
def list_suggestions(week: str | None = None, status: str | None = None, db: Session = Depends(get_db)):
    """改进建议（新的周在前）；week=YYYY-MM-DD 只看那一周。"""
    from ..models import ImprovementSuggestion

    q = select(ImprovementSuggestion).order_by(ImprovementSuggestion.week.desc(), ImprovementSuggestion.count.desc()).limit(300)
    if week:
        q = q.where(ImprovementSuggestion.week == week)
    if status:
        q = q.where(ImprovementSuggestion.status == status)
    return [_sugg(s) for s in db.scalars(q)]


@router.post("/suggestions/generate", dependencies=[Depends(require_admin)])
def generate_suggestions(request: Request, db: Session = Depends(get_db)):
    """现在就生成（刷新）这周的改进建议（规则，不调模型）；已确认 / 不采纳的保留。"""
    from ..evaluation import feedback
    from ..models import AuditEvent

    out = feedback.generate_suggestions(db)
    db.add(AuditEvent(actor=current_user(request).display_name[:64], action="EVAL_SUGGESTIONS_GENERATED",
                      detail={"count": len(out)}))
    db.commit()
    return [_sugg(s) for s in out]


class DecideBody(BaseModel):
    status: str = Field(pattern="^(ACCEPTED|REJECTED|PROPOSED)$")
    note: str | None = Field(None, max_length=2000)


@router.post("/suggestions/{sid}/decide", dependencies=[Depends(require_admin)])
def decide_suggestion(sid: int, body: DecideBody, request: Request, db: Session = Depends(get_db)):
    """人工确认：采纳 / 不采纳（采纳只是记录，提示词和知识库仍由人去改）。"""
    from ..db import utcnow
    from ..models import AuditEvent, ImprovementSuggestion

    s = db.get(ImprovementSuggestion, sid)
    if s is None:
        raise HTTPException(404, "建议不存在")
    who = current_user(request).display_name[:64]
    s.status, s.note, s.decided_by, s.decided_at = body.status, body.note, who, utcnow()
    db.add(AuditEvent(actor=who, action="EVAL_SUGGESTION_DECIDED", detail={"id": sid, "status": body.status, "note": body.note}))
    db.commit()
    return _sugg(s)


def _sugg(s) -> dict:
    from ..evaluation.feedback import EDIT_TYPES
    from ..intents import INTENTS

    return {"id": s.id, "week": s.week, "intent": s.intent, "intent_label": INTENTS.get(s.intent, (s.intent,))[0],
            "edit_type": s.edit_type, "edit_type_label": EDIT_TYPES.get(s.edit_type, s.edit_type), "target": s.target,
            "count": s.count, "avg_edit_ratio": s.avg_edit_ratio, "suggestion": s.suggestion, "examples": s.examples or [],
            "status": s.status, "decided_by": s.decided_by,
            "decided_at": s.decided_at.isoformat(timespec="seconds") + "Z" if s.decided_at else None, "note": s.note}


@router.get("/knowledge-gaps")
def knowledge_gaps(days: int = 30, db: Session = Depends(get_db)):
    """知识缺口候选：知识库没命中且被人工大改的会话（已脱敏）。"""
    from ..evaluation import feedback

    return feedback.knowledge_gaps(db, max(1, min(days, 180)))


@router.get("/knowledge-gaps/export", dependencies=[Depends(require_admin)])
def export_knowledge_gaps(days: int = 30, db: Session = Depends(get_db)):
    """候选知识条款导出成知识库 Excel（启用=否：人工改写成通用说法后再导入、启用）。"""
    import io

    from fastapi.responses import StreamingResponse

    from ..db import utcnow
    from ..evaluation import feedback
    from ..models import Store

    names = dict(db.execute(select(Store.id, Store.name)).all())
    wb = feedback.gaps_workbook(feedback.knowledge_gaps(db, max(1, min(days, 180))), names)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fn = f"knowledge_gaps_{utcnow():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# ------------------------------------------------------------------ 置信度校准（A6）
@router.get("/calibration")
def calibration(days: int = 30, db: Session = Depends(get_db)):
    """意图把握度分桶 vs 实际被改比例；按意图给自动发送候选。"""
    from ..evaluation import calibration as cal

    return cal.table(db, max(1, min(days, 365)))


# ------------------------------------------------------------------ 黄金集标签人工复核（管理员、售后）
from ..auth import require_roles  # noqa: E402
from ..models import Role  # noqa: E402

_reviewers = require_roles(Role.ADMIN, Role.AFTERSALES)


@router.get("/golden/review.csv")
def export_review(user=Depends(_reviewers)):
    """导出黄金集期望标签复核表（CSV，Excel 能直接打开）：复核后把 reviewed 列填 1 再导入。"""
    from fastapi.responses import Response

    from ..evaluation import golden, review

    cases = golden.load()
    if not cases:
        raise HTTPException(404, "还没有黄金集：先运行 python scripts/build_eval_set.py")
    return Response(review.export_csv(cases), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="golden_review.csv"'})


class ReviewImportBody(BaseModel):
    csv: str = Field(min_length=10, max_length=5_000_000)


@router.post("/golden/review")
def import_review(body: ReviewImportBody, db: Session = Depends(get_db), user=Depends(_reviewers)):
    """导入复核结果：reviewed=1 的行写回黄金集（label_source=human），记审计 EVAL_LABELS_IMPORTED。"""
    from ..evaluation import golden, review

    cases = golden.load()
    if not cases:
        raise HTTPException(404, "还没有黄金集")
    done, errors = review.import_csv(cases, body.csv)
    if done:
        golden.dump(cases)
    review.audit(db, user.display_name, done, errors)
    db.commit()
    return {"imported": len(done), "errors": errors, "distribution": review.distribution(cases)}


@router.get("/golden/summary")
def golden_summary():
    """黄金集如实统计：条数、来源、标签来源、待复核数。"""
    from ..evaluation import golden, review

    cases = golden.load()
    return {"cases": len(cases), **review.distribution(cases)}
