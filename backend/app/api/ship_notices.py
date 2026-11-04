"""发货通知：当天已发货订单的通知清单，审核后发给买家（模拟发送）。按负责店铺。"""
from __future__ import annotations

import threading
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import allowed_store_ids, check_store
from ..db import get_db
from ..models import ShipNotice, Store
from ..ship_notices import CN, NoticeError, conversation_for, counts, send, worker
from .deps import iso, reviewer_name

router = APIRouter(prefix="/internal/ship-notices", tags=["ship-notices"])


def _dict(n: ShipNotice, names: dict, conv_id: int | None) -> dict:
    return {"id": n.id, "store_id": n.store_id, "store_name": names.get(n.store_id), "order_id": n.order_id,
            "buyer_key": n.buyer_key, "marketplace_id": n.marketplace_id, "ship_date": n.ship_date,
            "shipped_at": iso(n.shipped_at), "packages": n.packages, "language": n.language, "text": n.text,
            "text_zh": n.text_zh, "status": n.status, "skip_reason": n.skip_reason, "conversation_id": conv_id,
            "sent_by": n.sent_by, "sent_at": iso(n.sent_at)}


def _load(db: Session, notice_id: int) -> ShipNotice:
    n = db.get(ShipNotice, notice_id)
    if n is None:
        raise HTTPException(404, "发货通知不存在")
    check_store(n.store_id)
    return n


@router.get("")
def list_notices(day: date | None = None, status: str | None = None, store_id: str | None = None, q: str | None = None,
                 db: Session = Depends(get_db)):
    """某天（默认北京时间今天）的发货通知；附各状态数量、最近一次汇总结果。买家已有会话的带上会话 ID。"""
    d = (day or datetime.now(CN).date()).isoformat()
    allowed = allowed_store_ids()
    query = select(ShipNotice).where(ShipNotice.ship_date == d)
    if allowed is not None:
        query = query.where(ShipNotice.store_id.in_(allowed))
    if store_id:
        query = query.where(ShipNotice.store_id == store_id)
    c = counts(db, d, list(allowed) if allowed is not None else None) if not store_id else counts(db, d, [store_id])
    if status:
        query = query.where(ShipNotice.status == status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(ShipNotice.order_id.ilike(like), ShipNotice.buyer_key.ilike(like)))
    rows = db.scalars(query.order_by(ShipNotice.status != "PENDING", ShipNotice.shipped_at.desc()).limit(1000)).all()
    names = dict(db.execute(select(Store.id, Store.name)).all())
    items = []
    for n in rows:
        conv = conversation_for(db, n, create=False)
        items.append(_dict(n, names, conv.id if conv else None))
    return {"date": d, "counts": c, "items": items, "collecting": _collecting.is_set(),
            "last": worker.last, "last_at": iso(worker.last_at), "auto": worker.running}


_collecting = threading.Event()


class CollectBody(BaseModel):
    day: date | None = None


@router.post("/collect")
def collect_now(body: CollectBody):
    """马上汇总一次（后台进行，几十个店铺要一两分钟）；页面刷新看结果。"""
    if _collecting.is_set():
        return {"started": False, "message": "正在汇总，请稍后刷新"}
    _collecting.set()

    def run():
        try:
            worker.run_now(body.day)
        finally:
            _collecting.clear()

    threading.Thread(target=run, name="ship-notices-now", daemon=True).start()
    return {"started": True}


class TextBody(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


@router.patch("/{notice_id}")
def edit_notice(notice_id: int, body: TextBody, db: Session = Depends(get_db)):
    n = _load(db, notice_id)
    if n.status != "PENDING":
        raise HTTPException(409, "已处理的发货通知不能修改")
    n.text = body.text.strip()
    return {"id": n.id, "text": n.text}


@router.post("/{notice_id}/send")
def send_one(notice_id: int, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    n = _load(db, notice_id)
    try:
        send(db, n, reviewer)
    except NoticeError as e:
        raise HTTPException(409, str(e)) from None
    return {"id": n.id, "status": n.status, "conversation_id": n.conversation_id}


class BatchBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/send-batch")
def send_batch(body: BatchBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """批量发送选中的待审核通知；单条失败不影响其他，失败原因逐条返回。"""
    sent, failed = 0, []
    for nid in body.ids:
        n = db.get(ShipNotice, nid)
        if n is None:
            continue
        try:
            check_store(n.store_id)
            with db.begin_nested():
                send(db, n, reviewer)
            sent += 1
        except (NoticeError, HTTPException) as e:
            failed.append({"id": nid, "order_id": n.order_id, "error": getattr(e, "detail", None) or str(e)})
    return {"sent": sent, "failed": failed}


class SkipBody(BaseModel):
    reason: str = Field(default="审核员跳过", max_length=200)


@router.post("/{notice_id}/skip")
def skip_notice(notice_id: int, body: SkipBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    n = _load(db, notice_id)
    if n.status != "PENDING":
        raise HTTPException(409, "已处理的发货通知不能跳过")
    n.status, n.skip_reason, n.sent_by = "SKIPPED", body.reason.strip() or "审核员跳过", reviewer
    return {"id": n.id, "status": n.status}
