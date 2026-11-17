"""主动发信（智能客服 → 自动发信）：规则开关和阈值、立即扫描（可预览）、触发记录、作废。只生成草稿，不发。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import proactive
from ..auth import current_user, require_admin
from ..db import get_db

router = APIRouter(prefix="/internal/proactive", tags=["proactive"])


@router.get("/rules")
def rules(db: Session = Depends(get_db)):
    cfg = proactive.get_rules(db)
    from ..config import get_settings

    return {"scan_minutes": get_settings().proactive_scan_minutes, "cooldown_days": proactive.COOLDOWN_DAYS,
            "rules": [{"key": k, "label": v["label"], "unit": v["unit"], **cfg[k]} for k, v in proactive.RULES.items()]}


class RulesBody(BaseModel):
    rules: dict


@router.put("/rules", dependencies=[Depends(require_admin)])
def save_rules(body: RulesBody, request: Request, db: Session = Depends(get_db)):
    try:
        cfg = proactive.set_rules(db, body.rules, current_user(request).display_name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    db.commit()
    return cfg


@router.post("/scan", dependencies=[Depends(require_admin)])
def scan(request: Request, dry_run: bool = True, rule: str | None = None, db: Session = Depends(get_db)):
    """立即扫描：dry_run=true（默认）只列候选和会跳过的原因；false 才生成草稿。rule 指定时不管开关只扫这一条。"""
    if rule and rule not in proactive.RULES:
        raise HTTPException(422, "没有这条规则")
    out = proactive.scan(db, dry_run=dry_run, rules=[rule] if rule else None)
    if not dry_run:
        from ..models import AuditEvent

        db.add(AuditEvent(actor=current_user(request).display_name[:64], action="PROACTIVE_SCAN_MANUAL",
                          detail={k: {"candidates": v["candidates"], "drafted": v["drafted"]} for k, v in out.items()}))
    db.commit()
    return out


@router.get("/notices")
def notices(limit: int = 200, db: Session = Depends(get_db)):
    return proactive.notices(db, max(1, min(limit, 500)))


@router.post("/notices/{job_id}/dismiss")
def dismiss(job_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        proactive.dismiss(db, job_id, current_user(request).display_name)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    db.commit()
    return {"ok": True}
