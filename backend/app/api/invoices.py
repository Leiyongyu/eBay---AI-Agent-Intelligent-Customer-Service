"""发票接口。

会话里（客服、售后、管理员，按负责的店铺）：看发票申请、生成发票、预览待发送的 PDF、审核后发邮件。
管理员（/api/invoices*）：发票留底列表、下载、导出，开票主体配置，邮箱测试。
留底规则：已发出（含模拟发送）或作废的发票 PDF 只有管理员能看。
"""
from __future__ import annotations

import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_store, current_user, require_admin
from ..config import get_settings
from ..db import get_db, utcnow
from ..invoicing import mailer
from ..invoicing.builder import DEFAULT_VAT_RATES, LANGUAGES
from ..invoicing.service import (
    InvoiceError,
    file_of,
    invoice_dict,
    issue,
    issuer_dict,
    mask_email,
    request_dict,
    notice_text,
    send,
    send_with_notice,
    sender_address,
    store_for_order,
    store_issuer,
    void,
)
from ..models import (
    AuditEvent,
    Conversation,
    Invoice,
    InvoiceIssuer,
    InvoiceRequest,
    InvoiceRequestStatus,
    InvoiceStatus,
    Role,
    Store,
)
from ..sources import SourceNotAvailable, get_source_for_store
from .deps import iso

router = APIRouter(prefix="/internal", tags=["invoices"])
admin = APIRouter(prefix="/api", tags=["invoices-admin"], dependencies=[Depends(require_admin)])

# 审核人（非管理员）能预览的状态：还没发出去的
REVIEWABLE = (InvoiceStatus.ISSUED, InvoiceStatus.SEND_FAILED)


def _err(e: InvoiceError):
    raise HTTPException(e.status_code, e.message)


def _conv(db: Session, conv_id: int) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    return conv


def _invoice(db: Session, inv_id: int) -> Invoice:
    inv = db.get(Invoice, inv_id)
    if inv is None:
        raise HTTPException(404, "发票不存在")
    check_store(inv.store_id)
    return inv


def _mail_status() -> dict:
    s = get_settings()
    return {"enabled": s.invoice_mail_enabled, "not_ready_reasons": s.invoice_mail_not_ready_reasons(),
            "host": s.invoice_smtp_host, "port": s.invoice_smtp_port, "user": s.invoice_smtp_user,
            "password_configured": bool(s.invoice_smtp_password), "bcc": s.invoice_mail_bcc,
            "from_name": s.invoice_mail_from_name}


# ------------------------------------------------------------------ 会话里的发票
@router.get("/conversations/{conv_id}/invoice")
def conversation_invoice(conv_id: int, request: Request, db: Session = Depends(get_db)):
    """发票申请、本会话开过的发票、买家的订单（只给订单号/日期/金额/标题，不含姓名地址）。"""
    is_admin = current_user(request).role == Role.ADMIN
    conv = _conv(db, conv_id)
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv.id))
    invoices = db.scalars(select(Invoice).where(Invoice.conversation_id == conv.id).order_by(Invoice.id.desc())).all()
    orders, orders_error = [], None
    try:
        for o in get_source_for_store(conv.store_id).find_orders(conv.store_id, conv.buyer_key)[:20]:
            orders.append({"order_id": o.get("order_id"), "created_at": o.get("created_at"), "total": o.get("total"),
                           "title": o.get("title"), "line_items": o.get("line_items")})
    except (SourceNotAvailable, NotImplementedError) as e:
        orders_error = str(e) or type(e).__name__
    issuer = store_issuer(db, conv.store_id)
    return {"request": request_dict(req), "invoices": [invoice_dict(i, admin=is_admin) for i in invoices],
            "orders": orders, "orders_error": orders_error, "test_conversation": conv.channel_source != "ebay",
            # 抬头自动取店铺 eBay 注册资料；开票主体只管税号税率，没配也能开普通发票
            "issuer": {"id": issuer.id, "name": issuer.name, "vat_ready": bool(issuer.vat_numbers)} if issuer else None,
            "mail": {"enabled": not get_settings().invoice_mail_not_ready_reasons(),
                     "not_ready_reasons": get_settings().invoice_mail_not_ready_reasons()},
            "languages": list(LANGUAGES)}


class RequestBody(BaseModel):
    email: str | None = Field(default=None, max_length=200)
    invoice_type: str | None = Field(default=None, pattern="^(NORMAL|VAT)$")  # 买家要的发票种类
    order_id: str | None = Field(default=None, max_length=64)
    buyer_company: str | None = Field(default=None, max_length=200)
    buyer_vat: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, pattern="^(OPEN|CANCELLED)$")


@router.put("/conversations/{conv_id}/invoice-request")
def save_request(conv_id: int, body: RequestBody, request: Request, db: Session = Depends(get_db)):
    """客服手动登记或修改发票申请（AI 没识别到邮箱、要改订单或填公司抬头时），或取消。"""
    user = current_user(request)
    conv = _conv(db, conv_id)
    if body.email and not mailer.valid_email(body.email):
        raise HTTPException(422, "邮箱格式不对")
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv.id))
    if req is None:
        req = InvoiceRequest(conversation_id=conv.id, store_id=conv.store_id, source="manual",
                             status=InvoiceRequestStatus.OPEN)
        db.add(req)
    for k in ("email", "order_id", "buyer_company", "buyer_vat", "invoice_type"):
        v = getattr(body, k)
        if v is not None:
            setattr(req, k, v.strip() or None)
    if body.status:
        req.status = body.status
    req.updated_at = utcnow()
    db.add(AuditEvent(actor=user.display_name, action="INVOICE_REQUEST_SAVED", store_id=conv.store_id,
                      conversation_id=conv.id, detail={"email": mask_email(req.email), "order_id": req.order_id,
                                                       "status": req.status}))
    db.flush()
    return request_dict(req)


class IssueBody(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    kind: str | None = Field(default=None, pattern="^(NORMAL|VAT)$")  # 普通发票 / 增值税发票；空按申请里买家要的
    language: str | None = Field(default=None, max_length=8)
    buyer_company: str | None = Field(default=None, max_length=200)
    buyer_vat: str | None = Field(default=None, max_length=64)


@router.post("/conversations/{conv_id}/invoices")
def issue_for_conversation(conv_id: int, body: IssueBody, request: Request, db: Session = Depends(get_db)):
    """按订单生成发票（PDF 留底）。生成后还要人工审核才发给买家。"""
    user = current_user(request)
    conv = _conv(db, conv_id)
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv.id))
    company = body.buyer_company if body.buyer_company is not None else (req.buyer_company if req else None)
    vat = body.buyer_vat if body.buyer_vat is not None else (req.buyer_vat if req else None)
    try:
        inv = issue(db, store_id=conv.store_id, order_id=body.order_id, actor=user.display_name, conversation=conv,
                    request=req, language=body.language, buyer_company=company, buyer_vat=vat, kind=body.kind)
    except InvoiceError as e:
        _err(e)
    return invoice_dict(inv, admin=user.role == Role.ADMIN)


class OrderIssueBody(IssueBody):
    store_id: str | None = Field(default=None, max_length=64)  # 不填：按订单号自动找店铺


@router.post("/invoices")
def issue_for_order(body: OrderIssueBody, request: Request, db: Session = Depends(get_db)):
    """直接按订单号开票（不经过会话）：店铺按订单号自动确定，只能给自己负责的店铺开。
    返回发票信息和 pdf_url；生成后同样留底，发给买家仍走“审核并发送”。"""
    user = current_user(request)
    order_id = body.order_id.strip()
    store_id = body.store_id or store_for_order(db, order_id)
    if not store_id:
        raise HTTPException(404, f"查不到订单 {order_id}（本地和各店铺都没有）")
    check_store(store_id)
    try:
        inv = issue(db, store_id=store_id, order_id=order_id, actor=user.display_name, language=body.language,
                    buyer_company=body.buyer_company, buyer_vat=body.buyer_vat, kind=body.kind)
    except InvoiceError as e:
        _err(e)
    return {**invoice_dict(inv, admin=user.role == Role.ADMIN), "pdf_url": f"/internal/invoices/{inv.id}/pdf"}


@router.get("/invoices/{inv_id}/pdf")
def invoice_pdf(inv_id: int, request: Request, download: bool = False, db: Session = Depends(get_db)):
    """未发出的发票给审核人预览；已发出、作废的留底只有管理员能看。"""
    user = current_user(request)
    inv = _invoice(db, inv_id)
    if user.role != Role.ADMIN and inv.status not in REVIEWABLE:
        raise HTTPException(403, "发票留底只有管理员能查看")
    path = file_of(inv)
    if not path.exists():
        raise HTTPException(404, "PDF 文件不存在")
    db.add(AuditEvent(actor=user.display_name, action="INVOICE_PDF_VIEWED", store_id=inv.store_id,
                      conversation_id=inv.conversation_id, detail={"number": inv.number}))
    return FileResponse(path, media_type="application/pdf", filename=f"{inv.number}.pdf",
                        content_disposition_type="attachment" if download else "inline")


class SendBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    confirmed: bool = False  # 审核人确认已核对发票内容和邮箱


@router.post("/invoices/{inv_id}/send")
def send_invoice(inv_id: int, body: SendBody, request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    inv = _invoice(db, inv_id)
    if not body.confirmed:
        raise HTTPException(422, "请先核对发票内容和买家邮箱，再确认发送")
    if inv.status == InvoiceStatus.SIMULATED and user.role != Role.ADMIN:
        raise HTTPException(403, "已模拟发送过的发票只有管理员能补发")
    try:
        inv = send(db, inv, body.email, user.display_name)
    except InvoiceError as e:
        _err(e)
    return invoice_dict(inv, admin=user.role == Role.ADMIN)


class VoidBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/invoices/{inv_id}/void")
def void_invoice(inv_id: int, body: VoidBody, request: Request, db: Session = Depends(get_db)):
    """作废：号码和 PDF 保留。还没发出去的审核人可以作废，发出去的只有管理员能作废。"""
    user = current_user(request)
    inv = _invoice(db, inv_id)
    if user.role != Role.ADMIN and inv.status not in REVIEWABLE:
        raise HTTPException(403, "已发出的发票只有管理员能作废")
    try:
        inv = void(db, inv, body.reason, user.display_name)
    except InvoiceError as e:
        _err(e)
    return invoice_dict(inv, admin=user.role == Role.ADMIN)


# ------------------------------------------------------------------ 发票页：所有发票申请统一在这里审核、发送
ACTIVE_REQUESTS = (InvoiceRequestStatus.WAITING_EMAIL, InvoiceRequestStatus.OPEN)


def _req(db: Session, req_id: int) -> InvoiceRequest:
    req = db.get(InvoiceRequest, req_id)
    if req is None:
        raise HTTPException(404, "发票申请不存在")
    check_store(req.store_id)
    return req


def _req_row(db: Session, req: InvoiceRequest, is_admin: bool) -> dict:
    conv = db.get(Conversation, req.conversation_id)
    latest = db.scalar(select(Invoice).where(Invoice.request_id == req.id, Invoice.status != InvoiceStatus.VOID)
                       .order_by(Invoice.id.desc()).limit(1))
    return {**request_dict(req), "store_id": req.store_id, "conversation_id": req.conversation_id,
            "buyer_key": conv.buyer_key if conv else None, "marketplace_id": conv.marketplace_id if conv else None,
            "conversation_title": conv.conversation_title if conv else None,
            "test_conversation": bool(conv and conv.channel_source != "ebay"),
            "created_at": iso(req.created_at), "invoice": invoice_dict(latest, admin=is_admin) if latest else None}


@router.get("/invoice-requests/count")
def count_requests(db: Session = Depends(get_db)):
    """导航角标：待审核发送、等买家邮箱的数量（按负责的店铺）。"""
    from ..auth import allowed_store_ids

    q = select(InvoiceRequest.status, func.count()).where(InvoiceRequest.status.in_(ACTIVE_REQUESTS))
    allowed = allowed_store_ids()
    if allowed is not None:
        q = q.where(InvoiceRequest.store_id.in_(allowed or ["-"]))
    counts = dict(db.execute(q.group_by(InvoiceRequest.status)).all())
    return {"open": counts.get(InvoiceRequestStatus.OPEN, 0), "waiting": counts.get(InvoiceRequestStatus.WAITING_EMAIL, 0)}


@router.get("/invoice-requests")
def list_requests(request: Request, status: str = "active", store_id: str | None = None, q: str | None = None,
                  limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    """status：active 待处理（待审核 + 等买家邮箱，默认）/ OPEN / WAITING_EMAIL / DONE / CANCELLED / all。"""
    from ..auth import allowed_store_ids

    is_admin = current_user(request).role == Role.ADMIN
    query = select(InvoiceRequest).order_by(
        (InvoiceRequest.status == InvoiceRequestStatus.OPEN).desc(), InvoiceRequest.updated_at.desc())
    if status == "active":
        query = query.where(InvoiceRequest.status.in_(ACTIVE_REQUESTS))
    elif status != "all":
        query = query.where(InvoiceRequest.status == status)
    allowed = allowed_store_ids()
    if allowed is not None:
        query = query.where(InvoiceRequest.store_id.in_(allowed or ["-"]))
    if store_id:
        query = query.where(InvoiceRequest.store_id == store_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.join(Conversation, Conversation.id == InvoiceRequest.conversation_id).where(
            InvoiceRequest.order_id.ilike(like) | InvoiceRequest.email.ilike(like) | Conversation.buyer_key.ilike(like))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.scalars(query.limit(min(max(limit, 1), 200)).offset(max(offset, 0))).all()
    return {"total": total, "items": [_req_row(db, r, is_admin) for r in rows],
            "status_labels": InvoiceRequestStatus.LABELS}


@router.get("/invoice-requests/{req_id}")
def request_detail(req_id: int, request: Request, db: Session = Depends(get_db)):
    """审核用：申请、开过的发票、买家的订单（不含姓名地址）、开票主体与邮件是否就绪、发件邮箱。"""
    req = _req(db, req_id)
    info = conversation_invoice(req.conversation_id, request, db)
    return {**info, "request": _req_row(db, req, current_user(request).role == Role.ADMIN),
            "invoices": [i for i in info["invoices"] if i.get("id") and _belongs(db, i["id"], req)],
            "sender": sender_address()}


def _belongs(db: Session, inv_id: int, req: InvoiceRequest) -> bool:
    inv = db.get(Invoice, inv_id)
    return bool(inv and (inv.request_id == req.id or inv.conversation_id == req.conversation_id))


@router.put("/invoice-requests/{req_id}")
def update_request(req_id: int, body: RequestBody, request: Request, db: Session = Depends(get_db)):
    req = _req(db, req_id)
    if body.status is None and body.email and req.status == InvoiceRequestStatus.WAITING_EMAIL:
        body.status = "OPEN"  # 人工填了邮箱：转为待审核
    return save_request(req.conversation_id, body, request, db)


@router.post("/invoice-requests/{req_id}/invoices")
def issue_for_request(req_id: int, body: IssueBody, request: Request, db: Session = Depends(get_db)):
    req = _req(db, req_id)
    return issue_for_conversation(req.conversation_id, body, request, db)


@router.get("/invoices/{inv_id}/notice")
def invoice_notice(inv_id: int, db: Session = Depends(get_db)):
    """发送后给买家的“请查收”消息默认文本（发票语言）和中文对照。"""
    inv = _invoice(db, inv_id)
    text, zh = notice_text(inv)
    return {"text": text, "text_zh": zh, "sender": sender_address()}


class ReviewSendBody(BaseModel):
    invoice_id: int
    email: str = Field(min_length=3, max_length=200)
    confirmed: bool = False  # 审核人确认已核对发票内容、买家邮箱和提醒消息
    message: str | None = Field(default=None, max_length=3000)  # 给买家的提醒消息；空用默认文本


@router.post("/invoice-requests/{req_id}/send")
def review_and_send(req_id: int, body: ReviewSendBody, request: Request, db: Session = Depends(get_db)):
    """人工审核后：发票发到买家邮箱，同时在买家会话里发消息提醒查收（写明发件邮箱）。"""
    user = current_user(request)
    req = _req(db, req_id)
    inv = _invoice(db, body.invoice_id)
    if not body.confirmed:
        raise HTTPException(422, "请先核对发票内容、买家邮箱和提醒消息，再确认发送")
    if inv.status == InvoiceStatus.SIMULATED and user.role != Role.ADMIN:
        raise HTTPException(403, "已模拟发送过的发票只有管理员能补发")
    try:
        notice = send_with_notice(db, req, inv, body.email, body.message, user.display_name)
    except InvoiceError as e:
        _err(e)
    return {"invoice": invoice_dict(inv, admin=user.role == Role.ADMIN), "notice": notice,
            "request": _req_row(db, req, user.role == Role.ADMIN)}


# ------------------------------------------------------------------ 管理员：发票留底
def _list_query(store_id, q, date_from, date_to, status):
    query = select(Invoice).order_by(Invoice.id.desc())
    if store_id:
        query = query.where(Invoice.store_id == store_id)
    if status:
        query = query.where(Invoice.status == status)
    if date_from:
        query = query.where(Invoice.issue_date >= date_from)
    if date_to:
        query = query.where(Invoice.issue_date <= date_to)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(Invoice.number.ilike(like) | Invoice.order_id.ilike(like) | Invoice.sent_to.ilike(like))
    return query


def _admin_row(inv: Invoice) -> dict:
    d = invoice_dict(inv, admin=False)
    snap = inv.snapshot or {}
    d["buyer"] = (snap.get("bill_to") or [""])[0]
    d["ship_country"] = snap.get("ship_country")
    return d


@admin.get("/invoices")
def list_invoices(store_id: str | None = None, q: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0,
                  db: Session = Depends(get_db)):
    from sqlalchemy import func

    query = _list_query(store_id, q, date_from, date_to, status)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = db.scalars(query.limit(min(max(limit, 1), 500)).offset(max(offset, 0))).all()
    return {"total": total or 0, "items": [_admin_row(i) for i in rows],
            "status_labels": InvoiceStatus.LABELS}


@admin.get("/invoices/export")
def export_invoices(store_id: str | None = None, q: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, status: str | None = None, request: Request = None,
                    db: Session = Depends(get_db)):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "发票清单"
    ws.append(["发票号", "开票日期", "店铺", "订单号", "买方", "收货国家", "币种", "总额", "税率%", "增值税",
               "状态", "发送到", "发送人", "发送时间", "开票人", "作废原因"])
    for inv in db.scalars(_list_query(store_id, q, date_from, date_to, status).limit(20000)):
        r = _admin_row(inv)
        ws.append([inv.number, inv.issue_date, inv.store_id, inv.order_id, r["buyer"], r["ship_country"],
                   inv.currency, inv.total, inv.vat_rate, inv.vat_amount, r["status_label"], inv.sent_to,
                   inv.sent_by, r["sent_at"], inv.created_by, inv.void_reason])
    buf = io.BytesIO()
    wb.save(buf)
    db.add(AuditEvent(actor=current_user(request).display_name, action="INVOICES_EXPORTED", detail={
        "store_id": store_id, "date_from": date_from, "date_to": date_to}))
    return StreamingResponse(io.BytesIO(buf.getvalue()),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=invoices_{date.today():%Y%m%d}.xlsx"})


@admin.get("/invoices/mail-status")
def mail_status():
    return _mail_status()


class MailTestBody(BaseModel):
    to: str = Field(min_length=3, max_length=200)


@admin.post("/invoices/mail-test")
def mail_test(body: MailTestBody, request: Request, db: Session = Depends(get_db)):
    """用 .env 里的企业邮箱发一封测试邮件（不管 INVOICE_MAIL_ENABLED），确认账号和客户端专用密码能用。"""
    s = get_settings()
    if not mailer.valid_email(body.to):
        raise HTTPException(422, "邮箱格式不对")
    missing = [r for r in s.invoice_mail_not_ready_reasons() if "INVOICE_MAIL_ENABLED" not in r]
    if missing:
        raise HTTPException(409, "；".join(missing))
    from email.message import EmailMessage
    from email.utils import formataddr

    msg = EmailMessage()
    msg["Subject"] = "发票邮箱测试 / Invoice mail test"
    msg["From"] = formataddr((s.invoice_mail_from_name or "", s.invoice_smtp_user))
    msg["To"] = body.to
    msg.set_content("这是客服系统发出的测试邮件：企业邮箱 SMTP 可以正常发信。\nThis is a test message.")
    try:
        mailer.send(msg)
    except mailer.MailError as e:
        raise HTTPException(502, str(e)) from None
    db.add(AuditEvent(actor=current_user(request).display_name, action="INVOICE_MAIL_TESTED",
                      detail={"to": mask_email(body.to)}))
    return {"ok": True}


@admin.get("/invoices/{inv_id}")
def invoice_detail(inv_id: int, db: Session = Depends(get_db)):
    inv = db.get(Invoice, inv_id)
    if inv is None:
        raise HTTPException(404, "发票不存在")
    return invoice_dict(inv, admin=True)


# ------------------------------------------------------------------ 管理员：开票主体
class IssuerBody(BaseModel):
    name: str = Field(default="", max_length=200)  # 可空：发票抬头默认自动取店铺 eBay 注册资料，这里只在读不到时备用
    address: str = Field(default="", max_length=1000)
    postcode: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=200)
    vat_numbers: dict[str, str] = Field(default_factory=dict)
    vat_rates: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_VAT_RATES))
    footer: str | None = Field(default=None, max_length=1000)
    is_active: bool = False
    store_ids: list[str] = Field(default_factory=list)


def _issuers(db: Session) -> list[dict]:
    stores = db.execute(select(Store.id, Store.name, Store.invoice_issuer_id)).all()
    out = []
    for i in db.scalars(select(InvoiceIssuer).order_by(InvoiceIssuer.id)):
        d = issuer_dict(i)
        d["store_ids"] = [s.id for s in stores if s.invoice_issuer_id == i.id]
        out.append(d)
    return out


def _apply_issuer(db: Session, i: InvoiceIssuer, body: IssuerBody, actor: str) -> None:
    clean = lambda m: {k.strip().upper(): v for k, v in m.items() if k.strip() and str(v).strip()}  # noqa: E731
    i.name, i.address = body.name.strip(), body.address.strip()
    i.postcode, i.phone = (body.postcode or "").strip() or None, (body.phone or "").strip() or None
    i.email, i.footer = (body.email or "").strip() or None, (body.footer or "").strip() or None
    i.vat_numbers = {k: str(v).strip() for k, v in clean(body.vat_numbers).items()}
    i.vat_rates = clean(body.vat_rates)
    i.is_active, i.updated_by, i.updated_at = body.is_active, actor, utcnow()
    db.flush()
    for s in db.scalars(select(Store).where(Store.invoice_issuer_id == i.id)):
        if s.id not in body.store_ids:
            s.invoice_issuer_id = None
    for sid in body.store_ids:
        s = db.get(Store, sid)
        if s is None:
            raise HTTPException(422, f"店铺 {sid} 不存在")
        s.invoice_issuer_id = i.id
    db.add(AuditEvent(actor=actor, action="INVOICE_ISSUER_SAVED", detail={
        "issuer_id": i.id, "name": i.name, "active": i.is_active, "stores": body.store_ids,
        "vat_numbers": i.vat_numbers, "vat_rates": i.vat_rates}))


@admin.get("/invoice-issuers")
def list_issuers(db: Session = Depends(get_db)):
    return {"items": _issuers(db), "default_vat_rates": DEFAULT_VAT_RATES}


@admin.post("/invoice-issuers")
def create_issuer(body: IssuerBody, request: Request, db: Session = Depends(get_db)):
    i = InvoiceIssuer(name=body.name, address=body.address)
    db.add(i)
    _apply_issuer(db, i, body, current_user(request).display_name)
    db.flush()
    return _issuers(db)


@admin.patch("/invoice-issuers/{issuer_id}")
def update_issuer(issuer_id: int, body: IssuerBody, request: Request, db: Session = Depends(get_db)):
    i = db.get(InvoiceIssuer, issuer_id)
    if i is None:
        raise HTTPException(404, "开票主体不存在")
    _apply_issuer(db, i, body, current_user(request).display_name)
    db.flush()
    return _issuers(db)


@admin.get("/invoice-issuers/prefill")
def prefill_issuer(store_id: str):
    """从店铺的 eBay 注册资料读公司名、地址、电话，给新建开票主体预填（税号 eBay 不提供，要手填）。"""
    from ..ebay.stores import is_ebay_store
    from ..sources import get_ebay_source

    if not is_ebay_store(store_id):
        raise HTTPException(409, "这个店铺没有对接 eBay")
    try:
        return get_ebay_source().registration(store_id)
    except SourceNotAvailable as e:
        raise HTTPException(502, f"读取 eBay 注册资料失败：{e}") from None
