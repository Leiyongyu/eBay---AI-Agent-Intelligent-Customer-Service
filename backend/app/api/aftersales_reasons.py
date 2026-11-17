"""订单售后原因分类接口（售后管理“售后原因”页 + 会话侧栏标记）。权限：管理员看全部，客服只看负责的店铺。

人工写法“售后原因 + 空格 + 备注”，如“产品问题 外壳开裂”；人工标记永远优先，AI（规则）重算不覆盖，撤销后回到 AI。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from .. import aftersales_reasons as ar
from ..auth import allowed_store_ids, check_store, current_user, require_admin
from ..db import get_db, utcnow
from ..models import AuditEvent, Conversation, EbayOrder, OrderAftersalesReason as R, Store
from .deps import iso

router = APIRouter(prefix="/internal/aftersales-reasons", tags=["aftersales-reasons"])
conv_router = APIRouter(prefix="/internal/conversations", tags=["aftersales-reasons"])


class MarkBody(BaseModel):
    reason: str | None = Field(None, description="原因代码或中文名（与 text 二选一）")
    note: str | None = Field(None, max_length=500)
    text: str | None = Field(None, max_length=600, description="“产品问题 XXXXXXX”：开头是 6 种中文名之一，空格后是备注")


class ConvMarkBody(MarkBody):
    order_id: str | None = Field(None, max_length=64, description="会话对应多张订单时必填")


def _parse(body: MarkBody) -> tuple[str, str | None]:
    try:
        if body.text and body.text.strip():
            return ar.parse_text(body.text)
        if body.reason:
            return ar.reason_code(body.reason), (" ".join((body.note or "").split()) or None)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    raise HTTPException(422, "请填写售后原因，如“产品问题 外壳开裂”")


def _day_start(d: date) -> datetime:
    """本机日期的 0 点 → UTC（数据库存 UTC）。"""
    return datetime.combine(d, time.min).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime]:
    hi = _day_start(date_to + timedelta(days=1)) if date_to else utcnow() + timedelta(days=1)
    lo = _day_start(date_from) if date_from else utcnow() - timedelta(days=ar.WINDOW_DAYS)
    return lo, hi


def _stores(db: Session, store_id: str | None, marketplace: str | None = None) -> list[str] | None:
    """当前账号能看的店铺 ∩ 筛选；None = 全部。"""
    allowed = allowed_store_ids()
    out = None if allowed is None else set(allowed)
    if store_id:
        out = {store_id} if out is None or store_id in out else set()
    if marketplace:
        mk = {s for (s,) in db.execute(select(Store.id).where(Store.default_marketplace_id == marketplace))}
        out = mk if out is None else out & mk
    return None if out is None else sorted(out)


def _ctx(db: Session, rows: list[R]) -> dict:
    """item_dict 要的附加信息：各店铺退货同步覆盖到哪（未分类再分用）、上线前历史会话的 eBay 会话 ID 和时间。"""
    from ..models import BaselineConversation

    ids = {i for r in rows for i in r.baseline_conversation_ids or []}
    info = {}
    if ids:
        for c in db.scalars(select(BaselineConversation).where(BaselineConversation.id.in_(ids))):
            info[c.id] = {"id": c.id, "ebay_conversation_id": c.ebay_conversation_id,
                          "first_message_at": iso(c.first_message_at), "last_message_at": iso(c.last_message_at)}
    return {"cover": ar.return_coverage(db),
            "baseline": {r.id: [info[i] for i in r.baseline_conversation_ids or [] if i in info] for r in rows}}


def item_dict(r: R, store_names: dict[str, str], ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    baseline, cover = ctx.get("baseline") or {}, ctx.get("cover")
    return {
        "id": r.id, "store_id": r.store_id, "store_name": store_names.get(r.store_id, r.store_id), "order_id": r.order_id,
        "conversation_id": r.conversation_id, "conversation_ids": r.conversation_ids or [], "buyer_key": r.buyer_key,
        "baseline_conversations": baseline.get(r.id, []),
        "unclassified_kind": ar.unclassified_kind(r, cover.get(r.store_id)) if cover is not None else None,
        "marketplace_id": r.marketplace_id, "skus": r.skus, "item_ids": r.item_ids, "order_created_at": iso(r.order_created_at),
        "order_total": r.order_total, "currency": r.currency, "refunded": bool(r.refunded), "refund_amount": r.refund_amount,
        "refund_currency": r.refund_currency, "refund_at": iso(r.refund_at),
        "events": [{**e, "kind_label": ar.KIND_LABELS.get(e.get("kind"), e.get("kind")),
                    "reason_label": _reason_label(e.get("reason"))} for e in r.events or []],
        "ai_reason": r.ai_reason, "ai_reason_label": ar.REASONS.get(r.ai_reason) if r.ai_reason else None,
        "ai_note": r.ai_note, "ai_basis": r.ai_basis or [], "ai_at": iso(r.ai_at),
        "manual_reason": r.manual_reason, "manual_reason_label": ar.REASONS.get(r.manual_reason) if r.manual_reason else None,
        "manual_note": r.manual_note, "manual_by": r.manual_by, "manual_at": iso(r.manual_at),
        "final_reason": r.final_reason, "reason_label": ar.REASONS.get(r.final_reason) if r.final_reason else "未分类",
        "status": r.status, "status_label": ar.STATUS_LABELS.get(r.status, r.status),
        "created_at": iso(r.created_at), "updated_at": iso(r.updated_at),
    }


def _reason_label(code: str | None) -> str | None:
    if not code:
        return None
    try:
        from ..case_labels import reason_label

        return reason_label(code) or code
    except Exception:  # noqa: BLE001
        return code


def _names(db: Session) -> dict[str, str]:
    return dict(db.execute(select(Store.id, Store.name)).all())


def _conv_match(conv_id: int):
    """conversation_id 等于它，或 conversation_ids（JSON 数组文本）里有它。"""
    ids, t = cast(R.conversation_ids, String), str(conv_id)
    return or_(R.conversation_id == conv_id, ids == f"[{t}]", ids.like(f"[{t},%"), ids.like(f"% {t},%"), ids.like(f"% {t}]"),
               ids.like(f"%,{t},%"), ids.like(f"%,{t}]"))


def _filtered(db: Session, store_id, reason, status, date_from, date_to, refunded, q, with_status: bool = True,
              conversation_id: int | None = None, marketplace_id: str | None = None):
    stores = _stores(db, store_id)
    query = select(R)
    if stores is not None:
        query = query.where(R.store_id.in_(stores))
    if conversation_id is not None:
        query = query.where(_conv_match(conversation_id))
    if marketplace_id:
        query = query.where(R.marketplace_id == marketplace_id)
    if reason:
        if reason.upper() == "UNCLASSIFIED" or reason == "未分类":
            query = query.where(R.final_reason.is_(None))
        else:
            try:
                query = query.where(R.final_reason == ar.reason_code(reason))
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
    if status and with_status:
        query = query.where(R.status == status.upper())
    if date_from:
        query = query.where(R.order_created_at >= _day_start(date_from))
    if date_to:
        query = query.where(R.order_created_at < _day_start(date_to + timedelta(days=1)))
    if refunded is not None:
        query = query.where(R.refunded.is_(refunded))
    text = (q or "").strip()
    if text:
        like = f"%{text.lower()}%"
        conds = [func.lower(R.order_id).like(like), func.lower(R.skus).like(like), func.lower(R.buyer_key).like(like)]
        if text.isdigit():
            conds.append(_conv_match(int(text)))
        query = query.where(or_(*conds))
    return query


@router.get("")
def list_reasons(store_id: str | None = None, reason: str | None = None, status: str | None = None,
                 date_from: date | None = None, date_to: date | None = None, refunded: bool | None = None,
                 q: str | None = None, conversation_id: int | None = None, marketplace_id: str | None = None,
                 unclassified_kind: str | None = None, page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    """unclassified_kind：missing_return_data / direct_refund / other，只看这一类未分类（隐含只看未分类）。"""
    page, page_size = max(1, page), min(max(1, page_size), 500)
    base = _filtered(db, store_id, reason, status, date_from, date_to, refunded, q,
                     conversation_id=conversation_id, marketplace_id=marketplace_id)
    order = (R.order_created_at.desc().nulls_last(), R.id.desc())
    if unclassified_kind:
        matched = _by_kind(db, base.where(R.final_reason.is_(None)).order_by(*order), unclassified_kind)
        total, rows = len(matched), matched[(page - 1) * page_size: page * page_size]
    else:
        total = db.scalar(select(func.count()).select_from(base.subquery()))
        rows = db.scalars(base.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    counts_q = _filtered(db, store_id, reason, None, date_from, date_to, refunded, q, with_status=False,
                         conversation_id=conversation_id, marketplace_id=marketplace_id).subquery()
    counts = {k: 0 for k in ar.STATUS_LABELS}  # 各状态数量：不受 status 参数影响，受其他筛选影响
    counts.update(dict(db.execute(select(counts_q.c.status, func.count()).group_by(counts_q.c.status)).all()))
    names = _names(db)
    ctx = _ctx(db, list(rows))
    return {"items": [item_dict(r, names, ctx) for r in rows], "total": total, "page": page, "page_size": page_size,
            "counts": {"all": sum(counts.values()), **counts},
            "reasons": [{"code": k, "label": v} for k, v in ar.REASONS.items()]}


def _by_kind(db: Session, query, kind: str) -> list[R]:
    """未分类再分只能在应用侧算（要看事件和店铺的退货同步范围）：未分类一般一两千行，取出来再筛。"""
    if kind not in ar.UNCLASSIFIED_KINDS:
        raise HTTPException(422, "unclassified_kind 只能是 " + " / ".join(ar.UNCLASSIFIED_KINDS))
    cover = ar.return_coverage(db)
    return [r for r in db.scalars(query) if ar.unclassified_kind(r, cover.get(r.store_id)) == kind]


@router.get("/options")
def options():
    """6 种原因代码和中文名、状态、事件类型（前端也有常量，这里给一份权威的）。"""
    return {"reasons": [{"code": k, "label": v} for k, v in ar.REASONS.items()],
            "statuses": [{"code": k, "label": v} for k, v in ar.STATUS_LABELS.items()],
            "unclassified_kinds": [{"code": k, "label": v} for k, v in ar.UNCLASSIFIED_KINDS.items()],
            "event_kinds": [{"code": k, "label": v} for k, v in ar.KIND_LABELS.items()]}


def _load(db: Session, rid: int) -> R:
    r = db.get(R, rid)
    allowed = allowed_store_ids()
    if r is None or (allowed is not None and r.store_id not in allowed):
        raise HTTPException(404, "记录不存在")
    return r


def _mark(db: Session, r: R, code: str, note: str | None, who: str, via: str) -> None:
    before = (r.manual_reason, r.manual_note)
    r.manual_reason, r.manual_note, r.manual_by, r.manual_at = code, (note or None) and note[:500], who[:64], utcnow()
    r.updated_at = utcnow()
    ar.apply_final(r)
    db.add(AuditEvent(actor=who[:64], action="AFTERSALES_REASON_MARKED", store_id=r.store_id, conversation_id=r.conversation_id,
                      detail={"order_id": r.order_id, "reason": code, "note": r.manual_note, "before": list(before), "via": via}))


@router.put("/{rid}")
def mark(rid: int, body: MarkBody, request: Request, db: Session = Depends(get_db)):
    r = _load(db, rid)
    code, note = _parse(body)
    _mark(db, r, code, note, current_user(request).display_name, "list")
    db.flush()
    return item_dict(r, _names(db), _ctx(db, [r]))


@router.delete("/{rid}/manual")
def unmark(rid: int, request: Request, db: Session = Depends(get_db)):
    r = _load(db, rid)
    who = current_user(request).display_name[:64]
    before = (r.manual_reason, r.manual_note)
    r.manual_reason = r.manual_note = r.manual_by = r.manual_at = None
    r.updated_at = utcnow()
    ar.apply_final(r)
    db.add(AuditEvent(actor=who, action="AFTERSALES_REASON_UNMARKED", store_id=r.store_id, conversation_id=r.conversation_id,
                      detail={"order_id": r.order_id, "before": list(before)}))
    db.flush()
    return item_dict(r, _names(db), _ctx(db, [r]))


@router.get("/stats")
def get_stats(store_id: str | None = None, date_from: date | None = None, date_to: date | None = None,
              marketplace_id: str | None = None, marketplace: str | None = None, min_orders: int = 5,
              sku_sort: str = "refund_rate", sku_limit: int = 200, db: Session = Depends(get_db)):
    """站点按店铺默认站点筛（订单本身不带站点）；日期按下单时间。"""
    lo, hi = _range(date_from, date_to)
    return ar.stats(db, _stores(db, store_id, marketplace_id or marketplace), lo, hi, max(1, min_orders), sku_sort,
                    min(max(1, sku_limit), 2000))


@router.post("/rebuild", dependencies=[Depends(require_admin)])
def rebuild():
    r = ar.rebuild_all()
    return {"updated": r.get("updated", 0), **r}


EXPORT_COLUMNS = (("店铺", "store_name"), ("订单号", "order_id"), ("对话 ID", "conversation_id"), ("全部对话 ID", "conversation_ids"),
                  ("买家", "buyer_key"), ("站点", "marketplace_id"), ("SKU", "skus"), ("下单时间", "order_created_at"),
                  ("订单金额", "order_total"), ("币种", "currency"), ("是否退款", "refunded"), ("退款金额", "refund_amount"),
                  ("退款币种", "refund_currency"), ("售后事件", "events"), ("最终原因", "reason_label"), ("状态", "status_label"),
                  ("未分类再分", "unclassified_kind_label"), ("AI 原因", "ai_reason_label"), ("AI 说明", "ai_note"), ("AI 依据", "ai_basis"), ("人工原因", "manual_reason_label"),
                  ("人工备注", "manual_note"), ("标记人", "manual_by"), ("标记时间", "manual_at"))


def _csv_cell(v):
    """防公式注入：文字以 = + - @ 或制表符、回车开头时前面加单引号（Excel 不会当公式执行）。"""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


@router.get("/export")
def export(store_id: str | None = None, reason: str | None = None, status: str | None = None,
           date_from: date | None = None, date_to: date | None = None, refunded: bool | None = None, q: str | None = None,
           conversation_id: int | None = None, marketplace_id: str | None = None, unclassified_kind: str | None = None,
           db: Session = Depends(get_db)):
    query = _filtered(db, store_id, reason, status, date_from, date_to, refunded, q,
                      conversation_id=conversation_id, marketplace_id=marketplace_id) \
        .order_by(R.order_created_at.desc().nulls_last(), R.id.desc())
    rows = _by_kind(db, query.where(R.final_reason.is_(None)), unclassified_kind) if unclassified_kind \
        else db.scalars(query).all()
    names = _names(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in EXPORT_COLUMNS])
    ctx = _ctx(db, list(rows))
    for r in rows:
        d = item_dict(r, names, ctx)
        d["refunded"] = "是" if d["refunded"] else "否"
        d["conversation_ids"] = " ".join(str(x) for x in d["conversation_ids"])
        d["ai_basis"] = " ".join(d["ai_basis"])
        d["unclassified_kind_label"] = ar.UNCLASSIFIED_KINDS.get(d.get("unclassified_kind") or "", "")
        d["events"] = "；".join(f"{e['kind_label']} {e.get('id')} {e.get('reason') or ''} {e.get('status') or ''}".strip()
                               for e in d["events"])
        w.writerow([_csv_cell(d.get(k)) for _, k in EXPORT_COLUMNS])
    name = f"aftersales_reasons_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- 会话侧栏
def conversation_orders(db: Session, conv: Conversation) -> list[str]:
    """会话对应的订单：同店铺同买家，会话关联的刊登在订单商品里（或按订单建的会话）；买家只有一张订单、会话没关联刊登时就是它；
    表里已经把这个会话记在某张订单上的也算。"""
    if not conv.buyer_key:
        mine = []
    else:
        mine = db.execute(select(EbayOrder.order_id, EbayOrder.item_ids, EbayOrder.item_id).where(
            EbayOrder.store_id == conv.store_id, EbayOrder.buyer == conv.buyer_key.lower())).all()
    hit = [o for o, items, item in mine if (conv.ebay_conversation_id or "") == f"ORDER-{o}"
           or (conv.item_id and conv.item_id in (items or item or "").split(","))]
    if not hit and not conv.item_id:  # 会话没关联刊登：这个买家在店铺的订单都可能是（一张就是它，多张要选）
        hit = [o for o, _, _ in mine]
    known = [o for o, ids in db.execute(select(R.order_id, R.conversation_ids).where(R.store_id == conv.store_id))
             if conv.id in (ids or [])]
    return list(dict.fromkeys(hit + known))


@conv_router.put("/{conv_id}/aftersales-reason")
def mark_from_conversation(conv_id: int, body: ConvMarkBody, request: Request, db: Session = Depends(get_db)):
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    code, note = _parse(body)
    order_id = (body.order_id or "").strip()
    if order_id:  # 订单必须是这个店铺的，且属于这个会话的买家（或已经对上这个会话）：防打错订单号、挂到别人的订单上
        o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == conv.store_id, EbayOrder.order_id == order_id))
        same_buyer = o is not None and (o.buyer or "").lower() == (conv.buyer_key or "").lower() and bool(conv.buyer_key)
        if o is None or not (same_buyer or order_id in conversation_orders(db, conv)):
            options_ = conversation_orders(db, conv) or [x for (x,) in db.execute(select(EbayOrder.order_id).where(
                EbayOrder.store_id == conv.store_id, EbayOrder.buyer == (conv.buyer_key or "").lower())
                .order_by(EbayOrder.created_at.desc()).limit(50))]
            raise HTTPException(422, {"message": "订单号不对：不是这个店铺的订单" if o is None
                                      else "这张订单不是这个会话的买家的，请重新选择", "orders": options_})
    if not order_id:
        candidates = conversation_orders(db, conv)
        if len(candidates) != 1:
            buyer_orders = [o for (o,) in db.execute(select(EbayOrder.order_id).where(
                EbayOrder.store_id == conv.store_id, EbayOrder.buyer == (conv.buyer_key or "").lower())
                .order_by(EbayOrder.created_at.desc()).limit(50))] if not candidates else candidates
            raise HTTPException(422, {"message": "这个会话对应多张订单，请选择订单" if candidates
                                      else "找不到这个会话对应的订单，请选择或填写订单号", "orders": buyer_orders})
        order_id = candidates[0]
    r = db.scalar(select(R).where(R.store_id == conv.store_id, R.order_id == order_id))
    if r is None:  # 订单还不在表里（AI 没发现售后信号）：新建一行
        now = utcnow()
        o = db.scalar(select(EbayOrder).where(EbayOrder.store_id == conv.store_id, EbayOrder.order_id == order_id))
        r = R(store_id=conv.store_id, order_id=order_id, conversation_id=conv.id, conversation_ids=[conv.id],
              buyer_key=(conv.buyer_key or "").lower() or None, marketplace_id=conv.marketplace_id, created_at=now,
              updated_at=now, refunded=bool(o and o.refunded))
        if o is not None:
            r.skus, r.item_ids = ar._skus(o), (o.item_ids or o.item_id)
            r.order_created_at, r.order_total, r.currency = o.created_at, o.total, o.currency
        db.add(r)
        db.flush()
    elif conv.id not in (r.conversation_ids or []):
        r.conversation_ids = [*(r.conversation_ids or []), conv.id]
        r.conversation_id = r.conversation_id or conv.id
    _mark(db, r, code, note, current_user(request).display_name, f"conversation:{conv.id}")
    db.flush()
    return item_dict(r, _names(db), _ctx(db, [r]))
