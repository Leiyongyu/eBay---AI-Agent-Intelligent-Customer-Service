"""会话右侧“相关信息”的补充：所选订单的售后状态、物流详情（谷仓仓库与轨迹）、查件单、发票、评价、商品图、黑名单、内部备注。

- 物流沿用 AI 查物流的同一套（tools.readonly.get_package_status：eBay 发货 + 谷仓出库单 + 17TRACK 轨迹 + 物流判断），
  按需加载（右侧面板打开、切换订单时才查），不影响会话主体加载速度；
- 内部备注只在本系统，按店铺 + 订单（或只按买家）记录，所有同事可见；
- 只读：不在 eBay 上做任何操作。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import check_store, current_user
from ..db import get_db, utcnow
from ..models import AuditEvent, Conversation, EbayCase, Invoice, InvoiceStatus, LogisticsTicket, OrderNote, Role
from .deps import iso

router = APIRouter(prefix="/internal", tags=["order-extras"])
CASE_LABELS = {"INQUIRY": "未收到货咨询", "CASE": "纠纷", "RETURN": "退货", "PAYMENT_DISPUTE": "付款纠纷",
               "CANCEL": "取消请求", "REFUND": "退款", "FEEDBACK": "评价"}


def _conv(db: Session, conv_id: int) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    return conv


def _logistics(db: Session, conv: Conversation, order_id: str) -> dict:
    from ..agent.schemas import RuntimeContext
    from ..sources import get_source_for_store
    from ..tools.readonly import OrderIdArgs, get_package_status

    ctx = RuntimeContext(store_id=conv.store_id, conversation_id=conv.id, buyer_key=conv.buyer_key,
                         marketplace_id=conv.marketplace_id, item_id=conv.item_id, job_id=0, run_generation=0)
    try:
        status, data, _ref, note = get_package_status(ctx, get_source_for_store(conv.store_id),
                                                      OrderIdArgs(order_id=order_id), db=db)
    except Exception as e:  # noqa: BLE001 — 物流查不到不影响其他信息
        return {"status": "ERROR", "error": f"{type(e).__name__}: {str(e)[:160]}"}
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    out = []
    for r in rows:
        wh = r.get("warehouse") or {}
        a = r.get("assessment") or {}
        trk = r.get("tracking") or {}
        events = trk.get("events") or []
        out.append({
            "carrier": r.get("carrier") or wh.get("carrier_name"), "tracking_number": r.get("tracking_number"),
            "carrier_url": r.get("carrier_website"),
            "warehouse_code": wh.get("warehouse_code"), "warehouse_order": wh.get("order_code"),
            "warehouse_status": wh.get("status_label"), "shipping_method": wh.get("shipping_method"),
            "shipped_at": wh.get("date_shipping") or r.get("event_time"), "picked_up_at": wh.get("ascan_time"),
            "delivered_at": a.get("delivered_at") or wh.get("dscan_time"), "abnormal": wh.get("abnormal_problem_reason"),
            "tracking_status": r.get("tracking_status"), "latest_event": r.get("latest_event"), "event_time": r.get("event_time"),
            "events": [{"time": e.get("time"), "location": e.get("location"), "description": e.get("description")}
                       for e in events[:30]],
            "stage": a.get("stage"), "stage_label": a.get("stage_label"), "needs_human": a.get("needs_human"),
            "days_without_update": a.get("days_without_update"), "tracking_note": r.get("tracking_note"),
        })
    return {"status": status, "note": note, "packages": out}


def _note_dict(n: OrderNote) -> dict:
    return {"id": n.id, "order_id": n.order_id, "text": n.text, "created_by": n.created_by, "created_at": iso(n.created_at)}


@router.get("/conversations/{conv_id}/order-extras")
def order_extras(conv_id: int, order_id: str | None = None, legacy_order_id: str | None = None,
                 item_ids: str | None = None, logistics: bool = True, need_consignee: bool = False,
                 buyer_cases: bool = False, refresh: bool = False, db: Session = Depends(get_db)):
    from ..cache_age import max_age
    from ..config import get_settings

    with max_age(0 if refresh else get_settings().panel_cache_seconds):
        return _order_extras(conv_id, order_id, legacy_order_id, item_ids, logistics, need_consignee, buyer_cases, db)


def _order_extras(conv_id, order_id, legacy_order_id, item_ids, logistics, need_consignee, buyer_cases, db):
    """item_ids：订单里的商品号（逗号分隔），用来对上咨询、纠纷、评价（它们记的是 商品号-交易号）和取商品图。
    need_consignee：eBay 订单里收件人姓名 / 地址 / 电话不全时，从谷仓出库单补（只显示，不写库）。
    buyer_cases：没查到订单时，列出这个买家在本店的全部售后记录。"""
    from .. import case_labels as L
    from ..customers import blacklist_dict, blacklist_entry
    from ..ebay_cases import KINDS
    from ..invoicing.builder import MARKETPLACE_DOMAIN

    conv = _conv(db, conv_id)
    buyer = (conv.buyer_key or "").lower()
    refs = [x for x in (order_id, legacy_order_id) if x]
    items = [x.strip() for x in (item_ids or "").split(",") if x.strip()]
    # 订单每一行的“商品号-交易号”：咨询、纠纷、取消单记的是它（多件商品的订单和旧格式订单号对不上）；
    # 取消单没有买家名，只能这样对。售前（没选订单）时用这个买家在本店的全部订单行
    from ..models import EbayOrderLine

    line_q = select(EbayOrderLine.order_id, EbayOrderLine.line_key).where(EbayOrderLine.store_id == conv.store_id)
    lines = db.execute(line_q.where(EbayOrderLine.order_id.in_(refs))).all() if refs else []
    if buyer_cases and buyer:
        lines += db.execute(line_q.where(EbayOrderLine.buyer == buyer).limit(500)).all()
    case_refs = list(dict.fromkeys([*refs, *(x for line in lines for x in line if x)]))
    # 售后记录：订单号或 商品号-交易号 对得上的；咨询 / 纠纷 / 评价没有订单号时按 买家 + 商品
    conds = [EbayCase.order_id.in_(case_refs)] if case_refs else []
    if items:
        conds.append((func.lower(EbayCase.buyer) == buyer) & EbayCase.item_id.in_(items))
    if buyer_cases and buyer:
        conds.append(func.lower(EbayCase.buyer) == buyer)
    cases = db.scalars(select(EbayCase).where(EbayCase.store_id == conv.store_id, or_(*conds))
                       .order_by(EbayCase.opened_at.desc())).all() if conds else []
    case_rows = [{"id": c.id, "kind": c.kind, "label": CASE_LABELS.get(c.kind) or KINDS.get(c.kind), "external_id": c.external_id,
                  "status": c.status, "is_open": c.is_open, "opened_at": iso(c.opened_at), "due_at": iso(c.due_at),
                  "overdue": bool(c.is_open and c.due_at and c.due_at < utcnow()), "amount": c.amount, "currency": c.currency,
                  "reason": c.reason, "comment": c.comment, "rating": c.rating,
                  "action_url": (c.detail or {}).get("action_url"),
                  # 页面直接显示的中文：状态、原因、买家期待（退货类型）、卖家需要做的动作
                  "status_label": L.status_label(c.status), "reason_label": L.reason_label(c.reason),
                  "rating_label": L.status_label(c.rating),
                  "expect_label": L.detail_label("type", (c.detail or {}).get("type")) if (c.detail or {}).get("type") else None,
                  "seller_action_label": L.detail_label("seller_action", (c.detail or {}).get("seller_action"))
                  if (c.detail or {}).get("seller_action") else None} for c in cases]
    tickets = db.scalars(select(LogisticsTicket).where(LogisticsTicket.reference_no.in_(refs))
                         .order_by(LogisticsTicket.id.desc()).limit(10)).all() if refs else []
    invoices = db.scalars(select(Invoice).where(Invoice.store_id == conv.store_id, Invoice.order_id.in_(refs))
                          .order_by(Invoice.id.desc())).all() if refs else []
    notes = db.scalars(select(OrderNote).where(OrderNote.store_id == conv.store_id, OrderNote.buyer == buyer,
                                               or_(OrderNote.order_id.is_(None), OrderNote.order_id.in_(refs or [""])))
                       .order_by(OrderNote.id.desc())).all()
    pictures = {}
    if items:
        from ..sources import get_source_for_store

        src = get_source_for_store(conv.store_id)
        for iid in items[:5]:
            try:
                card = src.get_listing(conv.store_id, iid) or {}
                pictures[iid] = card.get("picture_url") or (card.get("pictures") or [None])[0]
            except Exception:  # noqa: BLE001
                pictures[iid] = None
    admin = current_user_is_admin()
    domain = MARKETPLACE_DOMAIN.get(conv.marketplace_id or "", "ebay.com")
    consignee = None
    if need_consignee and order_id:
        from ..logistics.goodcang import GoodcangClient, GoodcangError

        try:
            consignee = GoodcangClient().consignee_by_reference(order_id) or {"error": "谷仓没有这单的出库单"}
        except GoodcangError as e:
            consignee = {"error": str(e)[:200]}
    return {
        "order_id": order_id,
        # eBay 卖家后台的订单详情页（改地址、取消、退款都在那里人工操作；本系统不直接改 eBay 订单）
        "order_url": f"https://www.{domain}/sh/ord/details?orderid={order_id}" if order_id else None,
        "consignee": consignee,  # 谷仓出库单上的收件信息（eBay 没给全时兜底）
        "cases": [c for c in case_rows if c["kind"] != "FEEDBACK"],
        "feedback": [c for c in case_rows if c["kind"] == "FEEDBACK"],
        "logistics": _logistics(db, conv, order_id) if (logistics and order_id) else None,
        "tickets": [{"id": t.id, "io_code": t.io_code, "type": t.type_name, "status": t.io_status_text, "tracking_no": t.tracking_no,
                     "created_at": iso(t.created_at), "reply": (t.reply or "")[:300] or None, "replied_at": t.replied_at,
                     "send_status": t.send_status, "needs_human": t.needs_human} for t in tickets],
        "invoices": [{"id": i.id, "number": i.number, "kind": i.kind, "status": i.status,
                      "status_label": InvoiceStatus.LABELS.get(i.status, i.status), "total": i.total, "currency": i.currency,
                      "sent_at": iso(i.sent_at), "created_at": iso(i.created_at),
                      "pdf_url": f"/internal/invoices/{i.id}/pdf" if admin or i.status in (InvoiceStatus.ISSUED, InvoiceStatus.SEND_FAILED) else None}
                     for i in invoices],
        "blacklist": blacklist_dict(blacklist_entry(db, conv.store_id, buyer)),
        "notes": [_note_dict(n) for n in notes],
        "pictures": pictures,
    }


def current_user_is_admin() -> bool:
    from ..auth import current

    u = current()
    return bool(u and u.role == Role.ADMIN)


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    order_id: str | None = None  # 空 = 记在买家上（这个店铺的所有订单都看得到）


@router.post("/conversations/{conv_id}/order-notes")
def add_note(conv_id: int, body: NoteBody, request: Request, db: Session = Depends(get_db)):
    conv = _conv(db, conv_id)
    n = OrderNote(store_id=conv.store_id, order_id=(body.order_id or "").strip() or None, buyer=(conv.buyer_key or "").lower(),
                  text=body.text.strip(), created_by=current_user(request).display_name[:64], created_at=utcnow())
    db.add(n)
    db.commit()
    return _note_dict(n)


@router.delete("/order-notes/{note_id}")
def delete_note(note_id: int, request: Request, db: Session = Depends(get_db)):
    """只能删自己写的备注（管理员都能删）。"""
    user = current_user(request)
    n = db.get(OrderNote, note_id)
    if n is None:
        raise HTTPException(404, "备注不存在")
    check_store(n.store_id)
    if user.role != Role.ADMIN and n.created_by != user.display_name:
        raise HTTPException(403, "只能删除自己写的备注")
    db.add(AuditEvent(actor=user.display_name[:64], action="ORDER_NOTE_DELETED", store_id=n.store_id,
                      detail={"order_id": n.order_id, "buyer": n.buyer, "text": n.text[:200]}))
    db.delete(n)
    db.commit()
    return {"removed": note_id}
