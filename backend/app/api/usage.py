"""用量统计接口：模型调用次数、token、耗时，按日期/用途/模型/店铺/会话汇总。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import usage
from ..db import get_db

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
def usage_summary(days: int = 7, start: date | None = None, end: date | None = None, store_id: str | None = None,
                  db: Session = Depends(get_db)):
    """默认最近 7 天（含今天，按本机日期）；也可传 start/end。最长 366 天。"""
    end = end or date.today()
    start = start or end - timedelta(days=max(days, 1) - 1)
    if start > end:
        raise HTTPException(422, "开始日期晚于结束日期")
    if (end - start).days > 365:
        raise HTTPException(422, "统计范围最长 366 天")
    return usage.summary(db, start, end, store_id or None)
