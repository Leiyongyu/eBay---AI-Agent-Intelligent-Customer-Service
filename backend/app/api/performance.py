"""绩效统计接口：按客服汇总回复量、响应时长、AI 草稿采纳和处理动作；上线效果统计（都只读、只读库）。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import launch_metrics, performance
from ..auth import allowed_store_ids, check_store, require_admin
from ..db import get_db

router = APIRouter(prefix="/api/performance", tags=["performance"])


@router.get("/summary")
def performance_summary(days: int = 7, start: date | None = None, end: date | None = None,
                        store_id: str | None = None, db: Session = Depends(get_db)):
    """默认最近 7 天（含今天，按本机日期）；也可传 start/end，最长 366 天。只统计当前账号负责的店铺。"""
    start, end, stores = _scope(days, start, end, store_id)
    return performance.summary(db, start, end, stores)


def _scope(days: int, start: date | None, end: date | None, store_id: str | None):
    """时间范围（本机日期闭区间，默认最近 days 天含今天，最长 366 天）和店铺范围（客服只看自己负责的店铺）。"""
    end = end or date.today()
    start = start or end - timedelta(days=max(days, 1) - 1)
    if start > end:
        raise HTTPException(422, "开始日期晚于结束日期")
    if (end - start).days > 365:
        raise HTTPException(422, "统计范围最长 366 天")
    if store_id:
        check_store(store_id)
        return start, end, [store_id]
    allowed = allowed_store_ids()
    return start, end, None if allowed is None else list(allowed)


@router.post("/baseline/refresh", dependencies=[Depends(require_admin)])
def refresh_baseline(db: Session = Depends(get_db)):
    """管理员：重新计算上线前基线快照（基线原始消息还完整时才会覆盖；补拉新批次 / 售后单后也会在打开页面时自动重算）。"""
    return {"results": launch_metrics.refresh_baseline_snapshots(db, force=True)}


@router.get("/launch")
def launch_summary(days: int = 7, start: date | None = None, end: date | None = None,
                   store_id: str | None = None, db: Session = Depends(get_db)):
    """上线效果：来信与回复、AI 草稿效果、客服效率、售后与物流、大模型成本；每项口径见返回的 notes。"""
    start, end, stores = _scope(days, start, end, store_id)
    return launch_metrics.summary(db, start, end, stores)
