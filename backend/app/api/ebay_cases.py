"""售后管理接口：咨询、纠纷、退货、付款纠纷、评价、取消交易、退款单（按店铺范围）。

eBay 上的数据只读（定时拉取）；本系统里另外记处理状态（待处理 / 处理中 / 已处理 / 暂不处理）、星标、标签、
内部备注、操作员（认领）和删除（隐藏）——这些只在本系统，不写回 eBay，拉取更新时也不覆盖。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..auth import allowed_store_ids, current_user, require_admin
from ..db import get_db, utcnow
from ..ebay_cases import KINDS, worker
from ..models import AuditEvent, Conversation, EbayCase, EbayCaseTag, Store
from .deps import iso

router = APIRouter(prefix="/internal/ebay-cases", tags=["ebay-cases"])
DUE_SOON = timedelta(hours=48)
STATES = ("PENDING", "PROCESSING", "DONE", "HOLD")
TAG_COLORS = ("primary", "success", "warning", "danger", "info")
SEARCH_FIELDS = {"external_id": EbayCase.external_id, "order_id": EbayCase.order_id, "buyer": EbayCase.buyer,
                 "item_id": EbayCase.item_id, "title": EbayCase.title}

# 处理状态页签：eBay 上已关闭的一律算已处理；开着的看本系统记的状态
VIEW_CONDS = {
    "pending": and_(EbayCase.is_open.is_(True),
                    or_(EbayCase.work_state.is_(None), EbayCase.work_state == "PENDING")),
    "processing": and_(EbayCase.is_open.is_(True), EbayCase.work_state.in_(("PROCESSING", "HOLD"))),
    "done": or_(EbayCase.is_open.is_(False), EbayCase.work_state == "DONE"),
}
RATING_OF = {"negative": "Negative", "neutral": "Neutral", "positive": "Positive"}


def _who(request: Request) -> str:
    return current_user(request).display_name[:64]


def _scoped(q, store_id: str | None = None):
    allowed = allowed_store_ids()
    if allowed is not None:
        q = q.where(EbayCase.store_id.in_(allowed))
    if store_id:
        q = q.where(EbayCase.store_id == store_id)
    return q


def _visible(q):
    return q.where(EbayCase.hidden_at.is_(None))


def _view_of(c: EbayCase) -> str:
    if not c.is_open or c.work_state == "DONE":
        return "done"
    return "processing" if c.work_state in ("PROCESSING", "HOLD") else "pending"


def _load_case(db: Session, case_id: int) -> EbayCase:
    c = db.get(EbayCase, case_id)
    allowed = allowed_store_ids()
    if c is None or (allowed is not None and c.store_id not in allowed):
        raise HTTPException(404, "记录不存在")
    return c


def _split(text: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,，;；\t]+", text or "") if x.strip()]


def _like(text: str, prefix: bool) -> str:
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")  # 下划线按字面匹配
    return f"{esc}%" if prefix else f"%{esc}%"


def _day_start(d: date) -> datetime:
    """本机日期的 0 点 → UTC（数据库存 UTC）。"""
    return datetime.combine(d, time.min).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


@router.get("/counts")
def counts(store_id: str | None = None, db: Session = Depends(get_db)):
    """每类：待处理数、新的（没看过的）数、总数。"""
    rows = db.execute(_visible(_scoped(select(EbayCase.kind, func.count(),
                                              func.sum(case((EbayCase.is_open.is_(True), 1), else_=0)),
                                              func.sum(case((EbayCase.seen_at.is_(None), 1), else_=0))),
                                       store_id)).group_by(EbayCase.kind)).all()
    by = {k: {"total": t, "open": int(o or 0), "new": int(n or 0)} for k, t, o, n in rows}
    return {"kinds": [{"kind": k, "label": v, **by.get(k, {"total": 0, "open": 0, "new": 0})} for k, v in KINDS.items()],
            "sync": {"last_at": iso(worker.last_at), "running": worker.running_now, "last": worker.last}}


@router.get("/view-counts")
def view_counts(kind: str, store_id: str | None = None, db: Session = Depends(get_db)):
    """一个类型页面的数量：页签（全部 / 待处理 / 处理中 / 已处理；评价是差评 / 中评 / 好评）、星标、每个标签。"""
    if kind not in KINDS:
        raise HTTPException(422, "没有这个类型")
    base = _visible(_scoped(select(EbayCase.id).where(EbayCase.kind == kind), store_id))

    def n(*conds) -> int:
        return db.scalar(select(func.count()).select_from(base.where(*conds).subquery())) or 0

    views = {"all": n(), **{k: n(c) for k, c in VIEW_CONDS.items()}}
    ratings = {k: n(EbayCase.rating == v) for k, v in RATING_OF.items()} if kind == "FEEDBACK" else {}
    tags: dict[str, int] = {}
    for (ids,) in db.execute(select(EbayCase.tag_ids).where(EbayCase.id.in_(base), EbayCase.tag_ids.is_not(None))):
        for t in ids or []:
            tags[str(t)] = tags.get(str(t), 0) + 1
    return {"views": views, "ratings": ratings, "starred": n(EbayCase.starred.is_(True)), "tags": tags}


@router.get("/options")
def options(kind: str, db: Session = Depends(get_db)):
    """筛选下拉的候选：eBay 状态、原因、操作员、暂不处理原因（只列这个类型里出现过的）。"""
    base = _visible(_scoped(select(EbayCase).where(EbayCase.kind == kind)))

    def distinct(col) -> list[str]:
        sub = base.with_only_columns(col).where(col.is_not(None)).distinct()
        return sorted({v for (v,) in db.execute(sub) if v})

    from ..case_labels import reason_label, status_label

    statuses, reasons = distinct(EbayCase.status), distinct(EbayCase.reason)
    return {"statuses": statuses, "reasons": reasons,
            # 下拉显示中文、传原值
            "status_labels": {v: status_label(v) for v in statuses}, "reason_labels": {v: reason_label(v) for v in reasons},
            "operators": distinct(EbayCase.operator), "hold_reasons": distinct(EbayCase.hold_reason),
            "stores": [{"id": i, "name": nme} for i, nme in db.execute(select(Store.id, Store.name).where(
                Store.id.in_(base.with_only_columns(EbayCase.store_id).distinct())).order_by(Store.name))]}


@router.get("")
def list_cases(kind: str, store_id: str | None = None, open_only: bool = False, q: str | None = None,
               q_field: str = "any", match: str = "contains", rating: str | None = None, view: str = "all",
               store_ids: str | None = None, status: str | None = None, reason: str | None = None,
               tag_id: int | None = None, tag_ids: str | None = None, tag_mode: str = "any",
               starred: bool = False, operator: str | None = None,
               hold_reason: str | None = None, opened_from: date | None = None, opened_to: date | None = None,
               updated_from: date | None = None, updated_to: date | None = None,
               limit: int = 300, db: Session = Depends(get_db)):
    """筛选（可组合）：q 编号/订单号/买家…（多个用逗号或换行分隔；q_field 选字段，match=prefix 开头 / contains 包含 / exact 完全一致）、
    view 页签、store_ids 多个店铺、status eBay 状态、reason 原因、tag_ids 多个标签（逗号分隔；tag_mode=any 有其一 /
    all 全都有；旧参数 tag_id 等于只选一个）、starred、operator（unassigned = 没人认领）、
    hold_reason、opened_from/to 创建日期、updated_from/to 最后处理（状态变化）日期。"""
    if kind not in KINDS:
        raise HTTPException(422, "没有这个类型")
    if tag_mode not in ("any", "all"):
        raise HTTPException(422, "tag_mode 只能是 any 或 all")
    try:
        want = {int(x) for x in _split(tag_ids)} | ({tag_id} if tag_id else set())
    except ValueError:
        raise HTTPException(422, "tag_ids 要是数字") from None
    query = _visible(_scoped(select(EbayCase).where(EbayCase.kind == kind), store_id))
    if want:  # 标签存在 JSON 里：先在这个类型里找出符合的记录，再和其他条件一起查（在 limit 之前过滤）
        hit = [cid for cid, ids in db.execute(select(EbayCase.id, EbayCase.tag_ids).where(
                   EbayCase.kind == kind, EbayCase.tag_ids.is_not(None)))
               if (want <= set(ids or []) if tag_mode == "all" else want & set(ids or []))]
        if not hit:
            return []
        query = query.where(EbayCase.id.in_(hit))
    if open_only:
        query = query.where(EbayCase.is_open.is_(True))
    if view in VIEW_CONDS:
        query = query.where(VIEW_CONDS[view])
    if rating:
        query = query.where(EbayCase.rating == RATING_OF.get(rating.lower(), rating))
    words = _split(q)
    if words:
        fields = [SEARCH_FIELDS[q_field]] if q_field in SEARCH_FIELDS else list(SEARCH_FIELDS.values())
        if match == "exact":
            conds = [func.lower(f) == w.lower() for f in fields for w in words]
        else:
            conds = [f.ilike(_like(w, match == "prefix"), escape="\\") for f in fields for w in words]
        query = query.where(or_(*conds))
    if store_ids:
        query = query.where(EbayCase.store_id.in_(_split(store_ids)))
    if status:
        query = query.where(EbayCase.status == status)
    if reason:
        query = query.where(EbayCase.reason == reason)
    if starred:
        query = query.where(EbayCase.starred.is_(True))
    if operator:
        query = query.where(EbayCase.operator.is_(None) if operator == "unassigned" else EbayCase.operator == operator)
    if hold_reason:
        query = query.where(EbayCase.hold_reason == hold_reason)
    if opened_from:
        query = query.where(EbayCase.opened_at >= _day_start(opened_from))
    if opened_to:
        query = query.where(EbayCase.opened_at < _day_start(opened_to + timedelta(days=1)))
    changed = func.coalesce(EbayCase.worked_at, EbayCase.status_changed_at, EbayCase.ebay_updated_at)
    if updated_from:
        query = query.where(changed >= _day_start(updated_from))
    if updated_to:
        query = query.where(changed < _day_start(updated_to + timedelta(days=1)))
    # 待处理的按截止时间（快到期的在前），其余按开始时间从新到旧
    query = query.order_by(EbayCase.is_open.desc(), EbayCase.due_at.is_(None), EbayCase.due_at, EbayCase.opened_at.desc())
    cases = list(db.scalars(query.limit(min(limit, 1000))))
    names = dict(db.execute(select(Store.id, Store.name)).all())
    buyers = {(c.store_id, (c.buyer or "").lower()) for c in cases if c.buyer}
    conv = {}
    if buyers:
        for cid, sid, key in db.execute(select(Conversation.id, Conversation.store_id, Conversation.buyer_key).where(
                Conversation.store_id.in_({b[0] for b in buyers}),
                func.lower(Conversation.buyer_key).in_({b[1] for b in buyers}))).all():
            conv.setdefault((sid, (key or "").lower()), cid)
    from ..customers import blacklist_map

    bl = blacklist_map(db, buyers)
    from ..case_extras import extras_for

    extras = extras_for(db, cases)  # 商品信息从库里批量取，前端不用再一行一行请求
    from ..case_labels import detail_labels, reason_label, status_label
    now = utcnow()
    return [{"id": c.id, "extras": extras.get(c.id), "blacklisted": (c.store_id, (c.buyer or "").lower()) in bl, "kind": c.kind, "store_id": c.store_id,
             "store_name": names.get(c.store_id) or c.store_id,
             "external_id": c.external_id, "order_id": c.order_id, "item_id": c.item_id, "buyer": c.buyer, "status": c.status,
             "is_open": c.is_open, "opened_at": iso(c.opened_at), "due_at": iso(c.due_at),
             "due_soon": bool(c.is_open and c.due_at and c.due_at - now <= DUE_SOON), "overdue": bool(c.is_open and c.due_at and c.due_at < now),
             "remaining_seconds": int((c.due_at - now).total_seconds()) if c.is_open and c.due_at else None,
             "amount": c.amount, "currency": c.currency, "reason": c.reason, "title": c.title, "comment": c.comment,
             "rating": c.rating, "detail": c.detail or {}, "is_new": c.seen_at is None,
             # 中文名：状态、原因、评价、详情里的其他字段（不认识的值原样）
             "status_label": status_label(c.status), "reason_label": reason_label(c.reason),
             "rating_label": status_label(c.rating), "detail_labels": detail_labels(c.detail),
             "status_changed_at": iso(c.status_changed_at), "first_seen_at": iso(c.first_seen_at),
             "ebay_updated_at": iso(c.ebay_updated_at),
             "view": _view_of(c), "work_state": c.work_state, "hold_reason": c.hold_reason,
             "starred": bool(c.starred), "tag_ids": c.tag_ids or [],
             "note": c.note, "note_by": c.note_by, "note_at": iso(c.note_at),
             "operator": c.operator, "worked_by": c.worked_by, "worked_at": iso(c.worked_at),
             "conversation_id": conv.get((c.store_id, (c.buyer or "").lower()))} for c in cases]


@router.get("/{case_id}/extras")
def case_extras(case_id: int, db: Session = Depends(get_db)):
    """一条售后单的商品信息：订单里这件商品的 SKU（平台 / 库存）、数量、单价、标题，以及商品主图。
    全部读库（订单商品明细、刊登信息每小时随售后同步更新），不实时查 eBay。列表接口已经带上了，这里给单条用。"""
    from ..case_extras import extras_for

    c = _load_case(db, case_id)
    return extras_for(db, [c])[c.id]


# ------------------------------------------------------------------ 本系统里的处理
class StarBody(BaseModel):
    starred: bool


class NoteBody(BaseModel):
    note: str = Field(default="", max_length=2000)


class BatchBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(state|star|unstar|tag_add|tag_remove|claim|release|hide)$")
    state: str | None = None  # action=state：PENDING / PROCESSING / DONE / HOLD
    hold_reason: str | None = Field(default=None, max_length=64)
    tag_id: int | None = None


def _apply(c: EbayCase, body: BatchBody, who: str, now: datetime) -> None:
    a = body.action
    if a == "state":
        c.work_state = None if body.state == "PENDING" else body.state
        c.hold_reason = (body.hold_reason or "").strip() or None if body.state == "HOLD" else None
        c.worked_by, c.worked_at = who, now
        if body.state in ("PROCESSING", "DONE") and not c.operator:
            c.operator, c.operator_at = who, now
    elif a in ("star", "unstar"):
        c.starred = a == "star"
    elif a == "tag_add":
        c.tag_ids = sorted({*(c.tag_ids or []), body.tag_id})
    elif a == "tag_remove":
        c.tag_ids = [t for t in (c.tag_ids or []) if t != body.tag_id] or None
    elif a == "claim":
        c.operator, c.operator_at = who, now
    elif a == "release":
        c.operator = c.operator_at = None
    elif a == "hide":
        c.hidden_at, c.hidden_by = now, who


@router.post("/batch")
def batch(body: BatchBody, request: Request, db: Session = Depends(get_db)):
    """批量：改处理状态（含暂不处理原因）、星标、加 / 去标签、认领 / 释放、删除（只在本系统隐藏）。"""
    if body.action == "state" and body.state not in STATES:
        raise HTTPException(422, "处理状态不对")
    if body.action in ("tag_add", "tag_remove"):
        if body.tag_id is None or db.get(EbayCaseTag, body.tag_id) is None:
            raise HTTPException(422, "标签不存在")
    who, now = _who(request), utcnow()
    done = 0
    for cid in dict.fromkeys(body.ids):
        c = _load_case(db, cid)
        _apply(c, body, who, now)
        done += 1
    db.add(AuditEvent(actor=who, action=f"EBAY_CASE_{body.action.upper()}",
                      detail={"ids": body.ids[:50], "count": done, "state": body.state, "tag_id": body.tag_id,
                              "hold_reason": body.hold_reason}))
    db.commit()
    return {"updated": done}


@router.put("/{case_id}/star")
def set_star(case_id: int, body: StarBody, db: Session = Depends(get_db)):
    c = _load_case(db, case_id)
    c.starred = body.starred
    db.commit()
    return {"starred": c.starred}


@router.put("/{case_id}/note")
def set_note(case_id: int, body: NoteBody, request: Request, db: Session = Depends(get_db)):
    """内部备注（同事共用，只在本系统）。空字符串 = 清掉。"""
    c = _load_case(db, case_id)
    c.note = body.note.strip() or None
    c.note_by, c.note_at = _who(request), utcnow()
    db.commit()
    return {"note": c.note, "note_by": c.note_by, "note_at": iso(c.note_at)}


@router.post("/{case_id}/seen")
def mark_seen(case_id: int, request: Request, db: Session = Depends(get_db)):
    c = _load_case(db, case_id)
    c.seen_at, c.seen_by = utcnow(), _who(request)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 快捷标签（每个类型一套）
class TagBody(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=32)
    color: str = "primary"


class TagPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = None
    sort: int | None = None
    pinned: bool | None = None


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _color_ok(c: str | None) -> bool:
    """颜色：任意 #rrggbb，或早期的 5 个名字。"""
    return bool(c) and (c in TAG_COLORS or bool(_HEX.match(c)))


def _tag_dict(t: EbayCaseTag) -> dict:
    return {"id": t.id, "kind": t.kind, "name": t.name, "color": t.color, "sort": t.sort,
            "pinned": t.pinned is not False, "created_by": t.created_by}


@router.get("/tags")
def list_tags(kind: str, db: Session = Depends(get_db)):
    return [_tag_dict(t) for t in db.scalars(select(EbayCaseTag).where(EbayCaseTag.kind == kind)
                                              .order_by(EbayCaseTag.sort, EbayCaseTag.id))]


def _name_taken(db: Session, kind: str, name: str, except_id: int | None = None) -> bool:
    q = select(EbayCaseTag.id).where(EbayCaseTag.kind == kind, func.lower(EbayCaseTag.name) == name.lower())
    if except_id is not None:
        q = q.where(EbayCaseTag.id != except_id)
    return db.scalar(q) is not None


@router.post("/tags")
def create_tag(body: TagBody, request: Request, db: Session = Depends(get_db)):
    """新建标签：所有人都可以（改名、改颜色、固定、删除只有管理员）。"""
    if body.kind not in KINDS:
        raise HTTPException(422, "没有这个类型")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "标签名不能为空")
    if _name_taken(db, body.kind, name):
        raise HTTPException(409, "已经有这个标签")
    sort = (db.scalar(select(func.max(EbayCaseTag.sort)).where(EbayCaseTag.kind == body.kind)) or 0) + 1
    t = EbayCaseTag(kind=body.kind, name=name, color=body.color.lower() if _color_ok(body.color) else "primary",
                    sort=sort, pinned=True, created_by=_who(request))
    db.add(t)
    db.add(AuditEvent(actor=_who(request), action="EBAY_CASE_TAG_CREATE",
                      detail={"kind": body.kind, "name": name, "color": t.color}))
    db.commit()
    return _tag_dict(t)


@router.patch("/tags/{tag_id}", dependencies=[Depends(require_admin)])
def update_tag(tag_id: int, body: TagPatch, request: Request, db: Session = Depends(get_db)):
    """管理员：改名、改颜色、排序、固定到快捷标签栏。"""
    t = db.get(EbayCaseTag, tag_id)
    if t is None:
        raise HTTPException(404, "标签不存在")
    before = _tag_dict(t)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "标签名不能为空")
        if _name_taken(db, t.kind, name, except_id=t.id):
            raise HTTPException(409, "已经有这个标签")
        t.name = name
    if body.color is not None:
        if not _color_ok(body.color):
            raise HTTPException(422, "颜色不对")
        t.color = body.color.lower()
    if body.sort is not None:
        t.sort = body.sort
    if body.pinned is not None:
        t.pinned = body.pinned
    db.add(AuditEvent(actor=_who(request), action="EBAY_CASE_TAG_UPDATE",
                      detail={"tag_id": t.id, "before": before, "patch": body.model_dump(exclude_none=True)}))
    db.commit()
    return _tag_dict(t)


@router.delete("/tags/{tag_id}", dependencies=[Depends(require_admin)])
def delete_tag(tag_id: int, request: Request, db: Session = Depends(get_db)):
    """管理员：删掉标签，并从所有记录上去掉。"""
    t = db.get(EbayCaseTag, tag_id)
    if t is None:
        raise HTTPException(404, "标签不存在")
    removed = 0
    for c in db.scalars(select(EbayCase).where(EbayCase.kind == t.kind, EbayCase.tag_ids.is_not(None))):
        if tag_id in (c.tag_ids or []):
            c.tag_ids = [x for x in c.tag_ids if x != tag_id] or None
            removed += 1
    db.add(AuditEvent(actor=_who(request), action="EBAY_CASE_TAG_DELETE",
                      detail={"tag_id": t.id, "kind": t.kind, "name": t.name, "removed_from": removed}))
    db.delete(t)
    db.commit()
    return {"ok": True, "removed_from": removed}


@router.post("/sync", dependencies=[Depends(require_admin)])
def sync_now():
    """管理员：马上从 eBay 拉一次（后台进行，全部店铺约几分钟）。"""
    return {"started": worker.run_in_background(), "running": worker.running_now}
