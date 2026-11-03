"""数据维护：聊天记录保留期清理的状态与手动触发（第 18.2 节）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import retention
from ..config import get_settings
from ..db import get_db
from ..models import Store
from .deps import reviewer_name

router = APIRouter(prefix="/internal/maintenance", tags=["maintenance"])


@router.get("/retention")
def retention_status(db: Session = Depends(get_db)):
    s = get_settings()
    stores = [{"id": st.id, "name": st.name, "retention_days": st.retention_days,
               "effective_days": retention.store_days(st, s)} for st in db.scalars(select(Store).order_by(Store.id))]
    return {
        "days": s.data_retention_days,
        "cleanup_time": s.retention_cleanup_time,
        "dry_run": s.retention_dry_run,
        "audit_days": s.audit_retention_days,
        "scheduler_running": retention.scheduler.running,
        "next_run": retention.scheduler.next_run(),
        "stores": stores,
        "runs": retention.last_runs(db),
    }


class RunBody(BaseModel):
    dry_run: bool = True


@router.post("/retention/run")
def retention_run(body: RunBody, reviewer: str = Depends(reviewer_name)):
    """手动清理一次。dry_run=false 才真正删除（与 RETENTION_DRY_RUN 配置无关，由点按钮的人决定）。"""
    r = retention.run(dry_run=body.dry_run, actor=reviewer, lock_timeout=30)
    if not r.get("ok") and "stores" not in r:
        raise HTTPException(409, r.get("error") or "清理未执行")
    return r
