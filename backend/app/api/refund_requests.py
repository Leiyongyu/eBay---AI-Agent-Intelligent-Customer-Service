"""退款申请单、退款审核规则、取消交易的写操作接口（见 refund_requests.py、cancel_actions.py）。

  GET  /internal/refund-requests                 列表（status / source / store_id / q / 日期），带各状态数量
  GET  /internal/refund-requests/prefill         提交表单预填（case_id | conversation_id | order_id + store_id）
  GET  /internal/refund-requests/{id}            一张申请单
  POST /internal/refund-requests                 提交（命中“直接退款”规则且是管理员 / 售后时直接退）
  POST /internal/refund-requests/{id}/approve    审核通过 → 调 eBay 退款（管理员 / 售后；带确认金额）
  POST /internal/refund-requests/{id}/reject     审核不通过（要写原因）
  POST /internal/refund-requests/{id}/resolve    处理异常的核对结果（refunded / not_refunded）
  POST /internal/refund-requests/{id}/refresh    退款中的：实时查订单看 eBay 退完没有（只读）
  GET  /internal/refund-review-rules             审核规则（所有人可看）
  PUT  /internal/refund-review-rules             整表保存（管理员）
  POST /internal/ebay-cases/{case_id}/cancel/{approve|reject}   同意 / 拒绝买家的取消申请
  GET  /internal/order-cancellations/eligibility  能不能主动取消（store_id + order_id，eBay 实时只读）
  POST /internal/order-cancellations             卖家主动取消订单（管理员 / 售后）
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import cancel_actions as ca
from .. import refund_requests as rq
from ..auth import allowed_store_ids, check_store, current_user, require_admin
from ..case_actions_common import ActionError
from ..db import get_db
from ..models import EbayCase, RefundRequest as R, Store

router = APIRouter(prefix="/internal", tags=["refund-requests"])


def _fail(db: Session, e: ActionError):
    db.rollback()
    raise HTTPException(e.status_code, e.message) from None


def _names(db: Session) -> dict[str, str]:
    return dict(db.execute(select(Store.id, Store.name)).all())


def _load(db: Session, rid: int) -> R:
    rr = db.get(R, rid)
    allowed = allowed_store_ids()
    if rr is None or (allowed is not None and rr.store_id not in allowed):
        raise HTTPException(404, "退款申请不存在")
    return rr


def _day(d: date) -> datetime:
    return datetime.combine(d, time.min)


# ------------------------------------------------------------------ 退款申请单
@router.get("/refund-requests")
def list_requests(status: str | None = None, source: str | None = None, store_id: str | None = None, q: str | None = None,
                  date_from: date | None = None, date_to: date | None = None, limit: int = 500, db: Session = Depends(get_db)):
    base = select(R)
    allowed = allowed_store_ids()
    if allowed is not None:
        base = base.where(R.store_id.in_(allowed))
    if store_id:
        base = base.where(R.store_id == store_id)
    if source:
        base = base.where(R.source == source)
    if date_from:
        base = base.where(R.submitted_at >= _day(date_from))
    if date_to:
        base = base.where(R.submitted_at < _day(date_to + timedelta(days=1)))
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(R.order_id.ilike(like), R.buyer.ilike(like), R.case_external_id.ilike(like),
                              R.ebay_refund_id.ilike(like), R.submitted_by.ilike(like)))
    counts = dict(db.execute(base.with_only_columns(R.status, func.count()).group_by(R.status)).all())
    if status:
        base = base.where(R.status == status)
    rows = db.scalars(base.order_by(R.submitted_at.desc(), R.id.desc()).limit(min(limit, 1000))).all()
    names = _names(db)
    return {"items": [rq.to_dict(r, names) for r in rows],
            "counts": {"all": sum(counts.values()), **{s: counts.get(s, 0) for s in R.STATUS_LABELS}},
            "status_labels": R.STATUS_LABELS, "sources": rq.SOURCES,
            "reasons": [{"code": k, "label": v} for k, v in rq.REASONS.items()]}


@router.get("/refund-requests/prefill")
def prefill(request: Request, case_id: int | None = None, conversation_id: int | None = None, order_id: str | None = None,
            store_id: str | None = None, db: Session = Depends(get_db)):
    try:
        return rq.prefill(db, current_user(request), case_id=case_id, conversation_id=conversation_id, order_id=order_id,
                          store_id=store_id)
    except ActionError as e:
        _fail(db, e)


@router.get("/refund-requests/{rid}")
def get_request(rid: int, request: Request, db: Session = Depends(get_db)):
    rr = _load(db, rid)
    user = current_user(request)
    blocked = rq.cannot_act(db.get(Store, rr.store_id))
    return {**rq.to_dict(rr, _names(db)), "can_review": rq.is_senior(user), "blocked": blocked}


class CreateBody(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    store_id: str | None = Field(None, max_length=64)
    conversation_id: int | None = None
    case_id: int | None = None
    source: str | None = Field(None, max_length=16)
    full: bool = False
    amount: float
    currency: str = Field(min_length=3, max_length=8)
    reason: str | None = Field(None, max_length=32)
    lines: list[dict] | None = None
    comment: str | None = Field(None, max_length=1000)
    note: str | None = Field(None, max_length=2000)
    confirm_amount: float | None = None
    confirm_currency: str | None = Field(None, max_length=8)
    # 执行方式：order_issue_refund 按订单退 / post_order_return 通过退货退（return_id + return_payload 交给 1 号 return_actions）
    execution: str | None = Field(None, max_length=24)
    return_id: str | None = Field(None, max_length=64)
    return_payload: dict | None = None


@router.post("/refund-requests")
def create_request(body: CreateBody, request: Request, db: Session = Depends(get_db)):
    try:
        rr = rq.create(db, current_user(request), body.model_dump())
    except ActionError as e:
        _fail(db, e)
    db.commit()
    if rr.status in (R.REFUNDING, R.REFUNDED):
        rq.refresh_reasons_later(rr.store_id)
    return rq.to_dict(rr, _names(db))


class ApproveBody(BaseModel):
    confirm_amount: float | None = None
    confirm_currency: str | None = Field(None, max_length=8)
    note: str | None = Field(None, max_length=2000)


@router.post("/refund-requests/{rid}/approve")
def approve_request(rid: int, body: ApproveBody, request: Request, db: Session = Depends(get_db)):
    rr = _load(db, rid)
    try:
        rq.approve(db, rr, current_user(request), body.model_dump())
    except ActionError as e:
        _fail(db, e)
    db.commit()
    if rr.status in (R.REFUNDING, R.REFUNDED):
        rq.refresh_reasons_later(rr.store_id)
    return rq.to_dict(rr, _names(db))


class RejectBody(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.post("/refund-requests/{rid}/reject")
def reject_request(rid: int, body: RejectBody, request: Request, db: Session = Depends(get_db)):
    rr = _load(db, rid)
    try:
        rq.reject(db, rr, current_user(request), body.note)
    except ActionError as e:
        _fail(db, e)
    db.commit()
    return rq.to_dict(rr, _names(db))


class ResolveBody(BaseModel):
    outcome: str = Field(pattern="^(refunded|not_refunded)$")
    refund_id: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=2000)


@router.post("/refund-requests/{rid}/resolve")
def resolve_request(rid: int, body: ResolveBody, request: Request, db: Session = Depends(get_db)):
    rr = _load(db, rid)
    try:
        rq.resolve(db, rr, current_user(request), body.outcome, body.refund_id, body.note)
    except ActionError as e:
        _fail(db, e)
    db.commit()
    if rr.status == R.REFUNDED:
        rq.refresh_reasons_later(rr.store_id)
    return rq.to_dict(rr, _names(db))


@router.post("/refund-requests/{rid}/refresh")
def refresh_request(rid: int, db: Session = Depends(get_db)):
    rr = _load(db, rid)
    try:
        rq.refresh_live(db, rr)
    except ActionError as e:
        _fail(db, e)
    except Exception as e:  # noqa: BLE001  只读查询失败：不改状态
        raise HTTPException(502, f"查 eBay 订单失败：{str(e)[:200]}") from None
    db.commit()
    return rq.to_dict(rr, _names(db))


# ------------------------------------------------------------------ 审核规则（客服设置 → 退款审核）
@router.get("/refund-review-rules")
def get_rules(db: Session = Depends(get_db)):
    return {"rules": [rq.rule_dict(r) for r in rq.rules(db)], "default_mode": "review",
            "dispute_auto_refund": False,  # 纠纷升级后自动退款：不做，固定关闭
            "stores": [{"id": i, "name": n} for i, n in db.execute(select(Store.id, Store.name).order_by(Store.name))]}


class RulesBody(BaseModel):
    rules: list[dict] = Field(default_factory=list, max_length=200)


@router.put("/refund-review-rules", dependencies=[Depends(require_admin)])
def put_rules(body: RulesBody, request: Request, db: Session = Depends(get_db)):
    try:
        rq.save_rules(db, body.rules, current_user(request).display_name)
    except ActionError as e:
        _fail(db, e)
    db.commit()
    return get_rules(db)


# ------------------------------------------------------------------ 取消交易
class CancelRespondBody(BaseModel):
    confirm_order_id: str | None = Field(None, max_length=64)
    reason: str | None = Field(None, max_length=500)
    shipped_at: str | None = Field(None, max_length=40)
    tracking_number: str | None = Field(None, max_length=64)


@router.post("/ebay-cases/{case_id}/cancel/{action}")
def cancel_respond(case_id: int, action: str, body: CancelRespondBody, request: Request, db: Session = Depends(get_db)):
    c = db.get(EbayCase, case_id)
    allowed = allowed_store_ids()
    if c is None or (allowed is not None and c.store_id not in allowed):
        raise HTTPException(404, "记录不存在")
    try:
        r = ca.respond(db, c, action, body.model_dump(), current_user(request))
    except ActionError as e:
        _fail(db, e)
    db.commit()
    if action == "approve":
        rq.refresh_reasons_later(c.store_id)
    return r


@router.get("/order-cancellations/eligibility")
def cancel_eligibility(store_id: str, order_id: str, request: Request, db: Session = Depends(get_db)):
    check_store(store_id)
    user = current_user(request)
    blocked = ca.cac.cannot_act(db.get(Store, store_id))
    out = {"blocked": blocked, "can_create": not blocked and ca.allowed(user, "create"),
           "reasons": [{"code": k, "label": v} for k, v in ca.SELLER_REASONS.items()], "eligibility": None}
    if blocked:
        return out
    try:
        out["eligibility"] = ca.check_eligibility(db, store_id, order_id)
    except ActionError as e:
        out["eligibility_error"] = e.message
    return out


class CancelCreateBody(BaseModel):
    store_id: str | None = Field(None, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    conversation_id: int | None = None
    reason: str = Field(max_length=40)
    note: str | None = Field(None, max_length=500)
    confirm_order_id: str | None = Field(None, max_length=64)


@router.post("/order-cancellations")
def cancel_create(body: CancelCreateBody, request: Request, db: Session = Depends(get_db)):
    try:
        r = ca.create(db, current_user(request), body.model_dump())
    except ActionError as e:
        _fail(db, e)
    db.commit()
    store_id = body.store_id or (db.get(EbayCase, r["case_id"]).store_id if r.get("case_id") else None)
    if store_id:
        rq.refresh_reasons_later(store_id)
    return r
