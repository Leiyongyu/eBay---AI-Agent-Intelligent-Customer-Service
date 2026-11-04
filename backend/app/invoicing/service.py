"""发票：开具（生成 PDF 并留底）、审核后邮件发送、作废，以及买家给出邮箱时登记发票申请。

- 每次生成的 PDF 都写进 INVOICE_DIR/<店铺>/<年份>/<发票号>.pdf 并记 SHA-256，开出后不改；开错了作废，号码和文件保留。
- 订单数据只从 eBay 读取；真实 eBay 会话只能给本会话买家自己的订单开票。
- 只开普通发票（和 eBay 订单单据一样）；买家要增值税发票的转人工，系统不生成。
- INVOICE_AUTO_SEND=true 时普通发票不审核：买家给了邮箱、订单能确定就自动生成并直接发出；否则在发票页审核后发送。
- INVOICE_MAIL_ENABLED=false 时只记为模拟发送；INVOICE_TEST_RECIPIENT 填了时所有发票邮件改发到测试邮箱。
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import (
    AuditEvent,
    Conversation,
    Invoice,
    InvoiceIssuer,
    InvoiceRequest,
    InvoiceRequestStatus,
    InvoiceStatus,
    Store,
)
from . import mailer
from .builder import InvoiceDataError, build, last5

log = logging.getLogger(__name__)

KIND_LABELS = {"NORMAL": "普通发票", "VAT": "增值税发票"}
# 买家提到这些词时，建议开增值税发票（公司买家、要税号、要列明增值税）
VAT_WORDS = re.compile(r"\b(mwst|ust|ust-?id\w*|umsatzsteuer\w*|mehrwertsteuer\w*|steuer\s*nummer|steuersatz|vat|iva|tva|"
                       r"firma|company|gewerbe|b2b|netto)\b|增值税|税号|税率|公司", re.I)


AUTO_ACTOR = "AI（自动开票）"
VAT_NOTE = "买家要增值税发票：系统不开增值税发票，请人工开具后发给买家"


def suggest_type(texts: list[str]) -> str | None:
    return "VAT" if any(VAT_WORDS.search(t or "") for t in texts) else None


# 发票相关的词（买家来信或之前的回复里出现，才把买家发来的邮箱当成“接收发票的邮箱”）
INVOICE_WORDS = re.compile(r"invoice|rechnung|facture|fattura|factura|receipt|quittung|beleg|发票|收据", re.I)


class InvoiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message


def mask_email(v: str | None) -> str | None:
    """审计和日志里只记打码后的邮箱。"""
    if not v or "@" not in v:
        return v
    name, domain = v.split("@", 1)
    return f"{name[:1]}***@{domain}"


def issuer_dict(i: InvoiceIssuer) -> dict:
    return {"id": i.id, "name": i.name, "address": i.address, "postcode": i.postcode, "phone": i.phone,
            "email": i.email, "vat_numbers": i.vat_numbers or {}, "vat_rates": i.vat_rates or {}, "footer": i.footer,
            "is_active": i.is_active, "updated_by": i.updated_by,
            "updated_at": i.updated_at.isoformat(timespec="seconds") + "Z" if i.updated_at else None}


def store_issuer(db: Session, store_id: str) -> InvoiceIssuer | None:
    store = db.get(Store, store_id)
    issuer = db.get(InvoiceIssuer, store.invoice_issuer_id) if store and store.invoice_issuer_id else None
    return issuer if issuer and issuer.is_active else None


def file_of(inv: Invoice) -> Path:
    return get_settings().invoice_root / inv.file_path


def _next_number(db: Session, issue_date: str, order_id: str) -> str:
    base = f"{issue_date.replace('-', '')}-{last5(order_id)}"
    taken = set(db.scalars(select(Invoice.number).where(Invoice.number.like(f"{base}%"))))
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _order(store_id: str, order_id: str) -> dict:
    from ..ebay.stores import is_ebay_store
    from ..sources import SourceNotAvailable, get_ebay_source

    if not is_ebay_store(store_id):
        raise InvoiceError(409, "只有对接了 eBay 的店铺能按订单开票")
    try:
        order = get_ebay_source().invoice_order(store_id, order_id)
    except SourceNotAvailable as e:
        raise InvoiceError(502, f"读取 eBay 订单失败：{e}") from None
    if not order:
        raise InvoiceError(404, f"本店铺没有订单 {order_id}")
    return order


def registration(store_id: str) -> dict | None:
    """订单所属店铺的 eBay 注册资料（发票抬头）；不是 eBay 店铺返回 None，读取失败抛 SourceNotAvailable。"""
    from ..ebay.stores import is_ebay_store
    from ..sources import get_ebay_source

    return get_ebay_source().registration(store_id) if is_ebay_store(store_id) else None


def seller_info(db: Session, store_id: str, kind: str) -> tuple[dict, int, list[str]]:
    """发票抬头：公司名、注册人、地址、邮编自动取店铺 eBay 注册资料；税号、税率、页脚取开票主体（没配用默认税率）。
    返回 (卖方信息, 开票主体 ID（没有为 0）, 提醒)。"""
    from ..sources import SourceNotAvailable
    from .builder import DEFAULT_VAT_RATES

    issuer = store_issuer(db, store_id)
    info = issuer_dict(issuer) if issuer else {"name": "", "address": "", "postcode": None, "phone": None, "email": None,
                                              "vat_numbers": {}, "vat_rates": dict(DEFAULT_VAT_RATES), "footer": None}
    if kind == "VAT" and (issuer is None or not issuer.vat_numbers):
        raise InvoiceError(409, "增值税发票要用店铺的增值税号：请管理员在“发票 → 开票主体”里填好这个店铺的税号并启用")
    notes: list[str] = []
    try:
        reg = registration(store_id)
    except SourceNotAvailable as e:
        reg = None
        notes.append(f"读取店铺 eBay 注册资料失败（{e}），抬头用开票主体里填的")
    if reg and (reg.get("name") or reg.get("contact")):
        info.update(name=reg.get("name") or reg.get("contact"), contact=reg.get("contact"),
                    address=reg.get("address") or "", postcode=reg.get("postcode"), store_url=reg.get("store_url"))
    elif not info.get("name") or not info.get("address"):
        raise InvoiceError(502, "读不到店铺的 eBay 注册资料，开票主体里也没填公司名和地址，无法确定发票抬头")
    return info, issuer.id if issuer else 0, notes


def issue(db: Session, *, store_id: str, order_id: str, actor: str, conversation: Conversation | None = None,
          request: InvoiceRequest | None = None, language: str | None = None, buyer_company: str | None = None,
          buyer_vat: str | None = None, order: dict | None = None, kind: str | None = None) -> Invoice:
    """生成普通发票并留底。增值税发票系统不再生成（转人工开具）。order 只给测试注入；正式从 eBay 读。"""
    kind = kind or "NORMAL"
    if kind == "VAT":
        raise InvoiceError(409, "系统不再生成增值税发票：买家要增值税发票的请人工开具后发给买家")
    order_id = (order_id or "").strip()
    if not order_id:
        raise InvoiceError(422, "请填写订单号")
    seller, issuer_id, seller_notes = seller_info(db, store_id, kind)
    order = order or _order(store_id, order_id)
    order_id = order.get("orderId") or order_id
    warnings: list[str] = list(seller_notes)
    buyer_name = ((order.get("buyer") or {}).get("username") or "").lower()
    if conversation is not None:
        if conversation.channel_source == "ebay":
            if buyer_name != (conversation.buyer_key or "").lower():
                raise InvoiceError(403, "这笔订单不是本会话买家的，不能给他开票")
        else:
            warnings.append("测试会话：没有核对订单是否属于这个买家")

    store = db.get(Store, store_id)
    tz = ZoneInfo(store.timezone) if store and store.timezone else ZoneInfo("UTC")
    issue_date = datetime.now(tz).date().isoformat()
    try:
        doc = build(order, issuer=seller, seller_account=store.ebay_user_id if store else None,
                    issue_date=issue_date, language=language, buyer_company=(buyer_company or "").strip() or None,
                    buyer_vat=(buyer_vat or "").strip() or None, kind=kind,
                    store_name=store.name if store else None)
    except InvoiceDataError as e:
        raise InvoiceError(422, str(e)) from None
    doc["warnings"] = warnings + doc["warnings"]
    previous = db.scalar(select(Invoice.number).where(Invoice.store_id == store_id, Invoice.order_id == order_id,
                                                      Invoice.kind == doc["kind"], Invoice.status != InvoiceStatus.VOID).limit(1))
    if previous:
        doc["warnings"].insert(0, f"这笔订单已经开过发票 {previous}")

    from .pdf import render

    number = _next_number(db, issue_date, order_id)
    pdf = render(doc, number)
    rel = f"{store_id}/{issue_date[:4]}/{number}.pdf"
    path = get_settings().invoice_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:  # 不覆盖已有文件
        f.write(pdf)
    inv = Invoice(number=number, store_id=store_id, conversation_id=conversation.id if conversation else None,
                  request_id=request.id if request else None, order_id=order_id, issuer_id=issuer_id,
                  issue_date=issue_date, kind=doc["kind"], language=doc["language"], currency=doc["currency"], total=float(doc["total"]),
                  vat_rate=doc["vat_rate"], vat_amount=float(doc["vat_amount"]) if doc["vat_amount"] else None,
                  snapshot=doc, file_path=rel, sha256=hashlib.sha256(pdf).hexdigest(), status=InvoiceStatus.ISSUED,
                  created_by=actor[:64])
    db.add(inv)
    if request is not None:
        request.order_id, request.updated_at = order_id, utcnow()
        request.invoice_type = request.invoice_type or doc["kind"]
    db.add(AuditEvent(actor=actor[:64], action="INVOICE_ISSUED", store_id=store_id,
                      conversation_id=conversation.id if conversation else None,
                      detail={"number": number, "order_id": order_id, "total": doc["total"], "currency": doc["currency"]}))
    db.flush()
    return inv


def store_for_order(db: Session, order_id: str) -> str | None:
    """订单号所属店铺：到各店铺用只读订单接口并行查（只有所属店铺查得到）。"""
    from ..aftersales_mail.service import lookup_order_api

    found = lookup_order_api(order_id)
    return found[0] if found else None


def send(db: Session, inv: Invoice, email: str, actor: str) -> Invoice:
    """人工审核后发给买家。邮件未开启时记为模拟发送（不真正发出）。"""
    email = (email or "").strip()
    if not mailer.valid_email(email):
        raise InvoiceError(422, "买家邮箱格式不对")
    if inv.status not in (InvoiceStatus.ISSUED, InvoiceStatus.SEND_FAILED, InvoiceStatus.SIMULATED):
        raise InvoiceError(409, f"发票状态是“{InvoiceStatus.LABELS.get(inv.status, inv.status)}”，不能发送")
    path = file_of(inv)
    pdf = path.read_bytes() if path.exists() else b""
    if hashlib.sha256(pdf).hexdigest() != inv.sha256:
        raise InvoiceError(409, "留底的 PDF 文件不见了或被改动过，请重新生成")
    s = get_settings()
    test_to = (s.invoice_test_recipient or "").strip()
    to = test_to if test_to and mailer.valid_email(test_to) else email  # 测试期改发到测试邮箱
    msg = mailer.compose(to=to, number=inv.number, order_id=inv.order_id, language=inv.language,
                         sign=s.invoice_mail_from_name or ((inv.snapshot or {}).get("seller") or {}).get("name") or "",
                         pdf=pdf,
                         show_number=inv.kind == "VAT")
    simulated = bool(s.invoice_mail_not_ready_reasons())
    inv.sent_to, inv.sent_by = to[:200], actor[:64]
    if simulated:
        inv.status, inv.sent_at, inv.send_error = InvoiceStatus.SIMULATED, utcnow(), None
    else:
        try:
            mailer.send(msg)
            inv.status, inv.sent_at, inv.send_error = InvoiceStatus.SENT, utcnow(), None
        except mailer.MailError as e:
            inv.status, inv.send_error = InvoiceStatus.SEND_FAILED, str(e)[:500]
    if inv.status in (InvoiceStatus.SENT, InvoiceStatus.SIMULATED) and inv.conversation_id:
        req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == inv.conversation_id))
        if req is not None:
            req.status, req.updated_at = InvoiceRequestStatus.DONE, utcnow()
    db.add(AuditEvent(actor=actor[:64], action="INVOICE_SENT" if inv.status != InvoiceStatus.SEND_FAILED
                      else "INVOICE_SEND_FAILED", store_id=inv.store_id, conversation_id=inv.conversation_id,
                      detail={"number": inv.number, "to": mask_email(to), "simulated": simulated,
                              "error": inv.send_error,
                              **({"buyer_email": mask_email(email)} if to != email else {})}))
    db.flush()
    return inv


def void(db: Session, inv: Invoice, reason: str, actor: str) -> Invoice:
    if inv.status == InvoiceStatus.VOID:
        raise InvoiceError(409, "已经作废了")
    if not (reason or "").strip():
        raise InvoiceError(422, "请写作废原因")
    inv.status, inv.void_reason = InvoiceStatus.VOID, reason.strip()[:500]
    db.add(AuditEvent(actor=actor[:64], action="INVOICE_VOIDED", store_id=inv.store_id,
                      conversation_id=inv.conversation_id, detail={"number": inv.number, "reason": inv.void_reason}))
    db.flush()
    return inv


# ------------------------------------------------------------------ 买家给邮箱 → 发票申请
def detect_invoice_email(buyer_texts: list[str], history_texts: list[str], intents: list[str]) -> str | None:
    """本次买家来信里有邮箱地址，并且这是发票场景（意图是发票，或本次/之前的消息提到发票），返回该邮箱。"""
    found = [m.group(0) for t in buyer_texts for m in mailer.EMAIL_RE.finditer(t or "")]
    if not found:
        return None
    if "invoice_vat" in (intents or []) or any(INVOICE_WORDS.search(t or "") for t in [*buyer_texts, *history_texts]):
        return found[-1]
    return None


def ensure_waiting(db: Session, conv: Conversation, *, order_candidates: list[str],
                   buyer_texts: list[str] = ()) -> InvoiceRequest | None:
    """买家要发票、还没给邮箱：登记一条“等买家邮箱”的申请（已有申请就不动）。"""
    if db.scalar(select(InvoiceRequest.id).where(InvoiceRequest.conversation_id == conv.id)):
        return None
    req = InvoiceRequest(conversation_id=conv.id, store_id=conv.store_id, source="agent",
                         status=InvoiceRequestStatus.WAITING_EMAIL,
                         order_id=order_candidates[0] if len(order_candidates) == 1 else None,
                         invoice_type=suggest_type(list(buyer_texts)))
    if req.invoice_type == "VAT":
        req.note = VAT_NOTE
    db.add(req)
    db.add(AuditEvent(actor="agent", action="INVOICE_REQUESTED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"status": req.status, "orders": len(order_candidates)}))
    db.flush()
    return req


def record_request(db: Session, conv: Conversation, email: str, *, order_candidates: list[str],
                   source: str = "agent", buyer_texts: list[str] = (), trigger: str | None = None) -> InvoiceRequest:
    """登记（或更新）会话的发票申请；只有一笔订单且开启自动生成时，先把发票生成好等人工审核。
    发票可以重复开：买家每要一次（trigger = 这次的买家消息）就开一张新的并发出；同一条消息重跑（重新生成草稿等）不重复开、不重复发。"""
    req = db.scalar(select(InvoiceRequest).where(InvoiceRequest.conversation_id == conv.id))
    if req is None:
        req = InvoiceRequest(conversation_id=conv.id, store_id=conv.store_id, source=source)
        db.add(req)
    elif trigger and req.trigger_message_id == trigger and req.status == InvoiceRequestStatus.DONE:
        return req  # 这条买家消息已经开票发出过了
    new_ask = not trigger or req.trigger_message_id != trigger
    req.trigger_message_id = trigger or req.trigger_message_id
    req.email, req.status, req.updated_at = email[:200], InvoiceRequestStatus.OPEN, utcnow()
    req.invoice_type = req.invoice_type or suggest_type(list(buyer_texts))
    db.add(AuditEvent(actor=source, action="INVOICE_REQUESTED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"email": mask_email(email), "orders": len(order_candidates)}))
    db.flush()
    if len(order_candidates) == 1 and not req.order_id:
        req.order_id = order_candidates[0]
    elif len(order_candidates) > 1:
        req.note = "买家有多笔订单，请选择要开票的订单"
    s = get_settings()
    if req.invoice_type == "VAT":  # 增值税发票：系统不开，交人工
        req.note = VAT_NOTE
        db.flush()
        return req
    if req.order_id and (s.invoice_auto_generate or s.invoice_auto_send):
        has = db.scalar(select(Invoice.id).where(Invoice.request_id == req.id, Invoice.status != InvoiceStatus.VOID))
        pending = db.scalar(select(Invoice.id).where(Invoice.request_id == req.id, Invoice.status == InvoiceStatus.ISSUED))
        if pending is None and (new_ask or not has):  # 已有生成好没发出的就用它；买家再要一次就再开一张
            try:
                with db.begin_nested():
                    issue(db, store_id=conv.store_id, order_id=req.order_id, actor="agent", conversation=conv,
                          request=req)
                req.note = None
            except InvoiceError as e:
                req.note = f"自动生成发票失败：{e.message}"
            except Exception as e:  # noqa: BLE001 — 自动开票失败不能影响回复流程
                log.exception("自动生成发票失败")
                req.note = f"自动生成发票失败：{type(e).__name__}"
    if s.invoice_auto_send and req.status == InvoiceRequestStatus.OPEN:
        inv = db.scalar(select(Invoice).where(Invoice.request_id == req.id, Invoice.status == InvoiceStatus.ISSUED)
                        .order_by(Invoice.id.desc()).limit(1))
        if inv is not None:  # 普通发票不审核：直接发到买家邮箱（AI 的回复里会告诉买家已发出，请查收）
            try:
                send(db, inv, email, AUTO_ACTOR)
            except InvoiceError as e:
                req.note = f"自动发送发票失败：{e.message}"
            if inv.status == InvoiceStatus.SEND_FAILED:
                req.note = f"自动发送发票失败：{inv.send_error}"
    db.flush()
    return req


def request_dict(req: InvoiceRequest | None) -> dict | None:
    if req is None:
        return None
    return {"id": req.id, "email": req.email, "order_id": req.order_id, "buyer_company": req.buyer_company,
            "buyer_vat": req.buyer_vat, "invoice_type": req.invoice_type, "status": req.status,
            "status_label": InvoiceRequestStatus.LABELS.get(req.status, req.status), "source": req.source,
            "note": req.note, "updated_at": req.updated_at.isoformat(timespec="seconds") + "Z" if req.updated_at else None}


def invoice_dict(inv: Invoice, *, admin: bool) -> dict:
    """admin=False 时不含买家姓名地址（快照）；审核预览看 PDF。"""
    d = {"id": inv.id, "number": inv.number, "store_id": inv.store_id, "conversation_id": inv.conversation_id,
         "order_id": inv.order_id, "issue_date": inv.issue_date, "kind": inv.kind or "VAT",
         "kind_label": KIND_LABELS.get(inv.kind or "VAT"), "language": inv.language, "currency": inv.currency,
         "total": inv.total, "vat_rate": inv.vat_rate, "vat_amount": inv.vat_amount, "status": inv.status,
         "status_label": InvoiceStatus.LABELS.get(inv.status, inv.status), "created_by": inv.created_by,
         "created_at": inv.created_at.isoformat(timespec="seconds") + "Z" if inv.created_at else None,
         "sent_to": inv.sent_to, "sent_by": inv.sent_by,
         "sent_at": inv.sent_at.isoformat(timespec="seconds") + "Z" if inv.sent_at else None,
         "send_error": inv.send_error, "void_reason": inv.void_reason,
         "warnings": (inv.snapshot or {}).get("warnings") or []}
    if admin:
        d["snapshot"] = inv.snapshot
    return d


# ------------------------------------------------------------------ 审核发送：发邮件 + 在 eBay 消息里提醒买家查收
NOTICE = {
    "de": ("Guten Tag,\n\nwir haben Ihnen die Rechnung{ref} zu Ihrer Bestellung {order} soeben per E-Mail gesendet{sender}. "
           "Bitte prüfen Sie Ihren Posteingang und gegebenenfalls auch den Spam-Ordner.\n\nMit freundlichen Grüßen",
           " (Absender: {addr})"),
    "en": ("Hello,\n\nWe have just emailed you invoice{ref} for your order {order}{sender}. "
           "Please check your inbox, and your spam folder if you cannot find it.\n\nKind regards", " (sent from {addr})"),
    "fr": ("Bonjour,\n\nNous venons de vous envoyer par e-mail la facture{ref} de votre commande {order}{sender}. "
           "Merci de vérifier votre boîte de réception ainsi que vos courriers indésirables.\n\nCordialement",
           " (expéditeur : {addr})"),
    "it": ("Buongiorno,\n\nle abbiamo appena inviato via e-mail la fattura{ref} del suo ordine {order}{sender}. "
           "Controlli la posta in arrivo e, se necessario, anche la cartella spam.\n\nCordiali saluti", " (mittente: {addr})"),
    "es": ("Hola,\n\nAcabamos de enviarle por correo electrónico la factura{ref} de su pedido {order}{sender}. "
           "Revise su bandeja de entrada y, si no la encuentra, también la carpeta de spam.\n\nAtentamente",
           " (remitente: {addr})"),
}
NOTICE_ZH = "您好，\n\n我们刚通过邮件把订单 {order} 的发票{ref} 发给您了{sender}。请查看收件箱，找不到的话也看一下垃圾邮件。\n\n祝好"


def sender_address() -> str | None:
    return get_settings().invoice_smtp_user or None


def notice_text(inv: Invoice) -> tuple[str, str]:
    """给买家的“请查收”消息（发票语言）和中文对照；写上发件邮箱，方便买家在收件箱里找。"""
    text, sender = NOTICE.get(inv.language, NOTICE["en"])
    addr = sender_address()
    ref = f" {inv.number}" if inv.kind == "VAT" else ""  # 普通发票上不印发票号，只说订单号
    return (text.format(ref=ref, order=inv.order_id, sender=sender.format(addr=addr) if addr else ""),
            NOTICE_ZH.format(ref=ref, order=inv.order_id, sender=f"（发件邮箱：{addr}）" if addr else ""))


def send_with_notice(db: Session, req: InvoiceRequest, inv: Invoice, email: str, message: str | None,
                     actor: str) -> dict:
    """人工审核后：发票发到买家邮箱；发出（或模拟发出）后在买家会话里发一条消息提醒查收。
    消息发不出（如会话有新消息需先处理）不影响邮件，结果里说明，可在工作台补发。"""
    if inv.request_id not in (None, req.id) or inv.store_id != req.store_id:
        raise InvoiceError(409, "这张发票不属于这条申请")
    req.email = email.strip()[:200]
    send(db, inv, email, actor)
    notice = {"sent": False, "error": None, "text": None}
    if inv.status in (InvoiceStatus.SENT, InvoiceStatus.SIMULATED):
        req.status, req.updated_at = InvoiceRequestStatus.DONE, utcnow()
        conv = db.get(Conversation, req.conversation_id)
        text, zh = notice_text(inv)
        text = (message or "").strip() or text
        notice["text"] = text
        if conv is None:
            notice["error"] = "会话已不存在，没有发消息"
        else:
            from ..sending.service import SendError, simulate_send

            try:
                with db.begin_nested():
                    simulate_send(db, conv=conv, text=text, actor=actor, text_zh=zh if not message else None)
                notice["sent"] = True
            except SendError as e:
                notice["error"] = e.message
        if notice["error"]:
            req.note = f"发票已发出，但提醒买家的消息没发出：{notice['error']}"
        db.add(AuditEvent(actor=actor[:64], action="INVOICE_NOTICE_SENT" if notice["sent"] else "INVOICE_NOTICE_FAILED",
                          store_id=req.store_id, conversation_id=req.conversation_id,
                          detail={"number": inv.number, "error": notice["error"]}))
    db.flush()
    return notice
