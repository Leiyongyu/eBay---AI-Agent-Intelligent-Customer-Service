"""物流转人工后的两件事（回复流程落库时调用，失败不影响回复）：
1. 查件单：断更、轨迹异常、妥投未收到时，在本系统起草一张查件单（查件单号为 DRAFT- 开头）；同一订单或跟踪号已有未完结的不再起草。
   开了 LOGISTICS_TICKET_AUTO_SUBMIT 时直接提交到谷仓（save_type=2），否则客服在工作台核对、可改后点“提交到谷仓”。
   已有在跟的查件单时这条物流原因不再转人工；只有还没提交的草稿不算在跟（不能对买家说已查件），照旧转人工。
2. 补发库存：涉及补发（超时需判断补发、买家要求补发、妥投未收到）时查谷仓库存，
   原发货区域仓有货才能补发，没货只能协商退款——结果写进售后备注给人工参考，AI 不对买家说。
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Aftersales, AuditEvent, LogisticsTicket
from .goodcang import GoodcangClient, GoodcangError

log = logging.getLogger(__name__)
ACTOR = "AI（物流规则）"
DRAFT_TEXT = "草稿（待在系统里提交）"
SUBMITTED_TEXT = "已提交（等谷仓同步状态）"
DISCARDED_TEXT = "已废弃（未提交）"
DRAFT_PREFIX = "DRAFT-"
CLOSED = {8, 10}  # 谷仓查件状态：8 已完结、10 废弃（本系统废弃的草稿也记 10）

# 原因 → 想要的查件类型（按顺序取物流方式支持的第一个）
TICKET_TYPES = {
    "TRACKING_DELIVERED_NOT_RECEIVED": ("妥投未收到", "申请POD", "留言"),
    "TRACKING_RESHIP_REVIEW": ("断更", "留言"),
    "TRACKING_STALLED": ("断更", "留言"),
    "TRACKING_EXCEPTION": ("轨迹异常", "断更", "留言"),
}
RESHIP_REASONS = {"TRACKING_RESHIP_REVIEW", "TRACKING_STALLED", "TRACKING_LOST_DAMAGED", "RESHIP_REQUESTED", "EMPTY_PARCEL",
                  "TRACKING_DELIVERED_NOT_RECEIVED"}
_ALIAS_PREFIX = re.compile(r"^[A-Z]+-(DE|US|UK|GB)-(?=[A-Z]+-\d)", re.I)  # 平台别名前缀，如 ZMY-DE-
_ALIAS_SUFFIX = re.compile(r"-AMZUS\d*$", re.I)
_LINE = re.compile(r"^[A-Za-z]+(?=-\d)")


def warehouse_sku(sku: str | None) -> str | None:
    """商品 SKU → 谷仓商品 SKU：产品线前缀换成 JMH，如 BWM-50016-0101 → JMH-50016-0101（已是 JMH- 的不变）。"""
    s = _ALIAS_SUFFIX.sub("", _ALIAS_PREFIX.sub("", (sku or "").strip()))
    return _LINE.sub("JMH", s) if s else None


def is_local_draft(t: LogisticsTicket) -> bool:
    """本系统起草、还没提交到谷仓的查件单（可在工作台修改、提交、废弃）。"""
    return (t.io_code or "").startswith(DRAFT_PREFIX) and t.io_status not in CLOSED


def is_draft(t: LogisticsTicket) -> bool:
    """还没提交的查件单：本系统草稿，或在谷仓后台保存、同步回来状态是草稿/待提交的。"""
    if is_local_draft(t):
        return True
    return t.io_status not in CLOSED and bool(re.search(r"草稿|待提交", t.io_status_text or ""))


# ---------------------------------------------------------------- 从本次工具结果里取包裹
def _packages(state) -> list[dict]:
    out = []
    for o in state["observations"]:
        if o["tool_name"] == "get_package_status" and isinstance(o.get("data"), (dict, list)):
            out += [r for r in (o["data"] if isinstance(o["data"], list) else [o["data"]]) if isinstance(r, dict)]
    return out


def _order(state) -> dict | None:
    for o in state["observations"]:
        if o["tool_name"] == "get_authorized_order_context" and o["status"] == "FOUND" and isinstance(o.get("data"), dict):
            return o["data"]
    return None


# ---------------------------------------------------------------- 查件单草稿
def _pick_type(client: GoodcangClient, sm_code: str | None, wanted: tuple[str, ...]) -> tuple[int, str] | None:
    types = client.ticket_types(sm_code)
    by_name = {(t.get("name") or t.get("it_type_name") or "").strip(): t.get("value", t.get("it_type_id")) for t in types}
    for name in wanted:
        if by_name.get(name) is not None:
            return int(by_name[name]), name
    return None


def _desc(reason: str, order_id: str, pkg: dict) -> str:
    a, tr = pkg.get("assessment") or {}, pkg.get("tracking") or {}
    ev = (tr.get("events") or [{}])[0]
    last = " ".join(x for x in (ev.get("time"), ev.get("location"), ev.get("description")) if x)
    head = f"eBay订单{order_id}，跟踪号{pkg.get('tracking_number')}（{pkg.get('carrier') or '承运商'}）"
    if reason == "TRACKING_DELIVERED_NOT_RECEIVED":
        body = "轨迹显示已妥投，买家反馈没有收到。请向承运商核实投递地址和签收情况，并提供POD（签收证明）。"
    elif reason == "TRACKING_EXCEPTION":
        body = f"轨迹显示异常（{last or tr.get('status_label') or '无详情'}）。请向承运商核实包裹当前状态。"
    else:
        days = a.get("days_without_update")
        body = (f"轨迹已{days:g}天未更新" if isinstance(days, (int, float)) else "轨迹长时间未更新") + \
               (f"，最后一条：{last}" if last else "") + "。请向承运商核实包裹当前状态并催派。"
    return (head + body)[:300]


OPEN_SKIP = "已有未完结的查件单"  # 已提交到谷仓、在跟进中：算已处理
DRAFT_SKIP = "已有查件单草稿（未提交到谷仓）"  # 还没提交：不能对买家说已查件，照旧转人工
STALE_SKIP = "查件单跟进超时"  # 谷仓回复（或提交）已超过 LOGISTICS_TICKET_FOLLOWUP_DAYS 天，问题还在：转人工判断补发


def ticket_age_days(t: LogisticsTicket) -> float | None:
    """查件单最近一次进展（谷仓回复，没有回复时按提交/创建时间）距今多少天。"""
    from datetime import datetime, timezone

    from .assess import CN, parse_time

    ref = parse_time(t.replied_at, CN) or (t.submitted_at.replace(tzinfo=timezone.utc) if t.submitted_at else None)         or parse_time(t.added_at, CN)
    return round((datetime.now(timezone.utc) - ref).total_seconds() / 86400, 1) if ref else None


def is_stale(t: LogisticsTicket) -> bool:
    """已提交的查件单跟进超时：谷仓回复后（或提交后一直没回复）超过 N 天，包裹还是同样的问题。"""
    if is_draft(t):
        return False
    age = ticket_age_days(t)
    return age is not None and age >= get_settings().logistics_ticket_followup_days


def _open_ticket(db: Session, order_id: str, tracking_no: str) -> LogisticsTicket | None:
    """同一订单或跟踪号未完结的查件单；已提交的优先返回。"""
    rows = [r for r in db.scalars(select(LogisticsTicket).where(or_(LogisticsTicket.tracking_no == tracking_no,
                                                                    LogisticsTicket.reference_no == order_id)))
            if r.io_status not in CLOSED]
    return next((r for r in rows if not is_draft(r)), rows[0] if rows else None)


def draft_tickets(db: Session, client: GoodcangClient, state, reasons: list[str], *, store_id: str | None,
                  buyer_key: str | None, conversation_id: int | None) -> list[dict]:
    s = get_settings()
    order = _order(state)
    out = []
    for pkg in _packages(state):
        reason = (pkg.get("assessment") or {}).get("reason_code")
        delivered = (pkg.get("tracking") or {}).get("status") == "Delivered" or \
            (pkg.get("assessment") or {}).get("stage") == "DELIVERED"  # 含谷仓回传妥投（17TRACK 查不到的承运商）
        if reason not in TICKET_TYPES and "TRACKING_DELIVERED_NOT_RECEIVED" in reasons and delivered:
            reason = "TRACKING_DELIVERED_NOT_RECEIVED"
        if reason not in TICKET_TYPES or reason not in reasons:
            continue
        tn, wh = pkg.get("tracking_number"), pkg.get("warehouse") or {}
        order_id = pkg.get("order_id") or (order or {}).get("order_id")
        if not (tn and wh.get("order_code") and order_id):
            out.append({"tracking_no": tn, "skipped": "不是谷仓发的货或缺跟踪号"})
            continue
        existing = _open_ticket(db, order_id, tn)
        if existing is not None:
            # 查件单在跟但已超时（谷仓回复“已催派”后轨迹还是不动，查件单我们不会去完结）：不再当成已处理，转人工判断补发
            skip = DRAFT_SKIP if is_draft(existing) else STALE_SKIP if is_stale(existing) else OPEN_SKIP
            out.append({"tracking_no": tn, "existing": existing.io_code, "reason": reason, "skipped": skip,
                        **({"age_days": ticket_age_days(existing)} if skip == STALE_SKIP else {})})
            continue
        wanted = TICKET_TYPES[reason]
        trk = pkg.get("tracking") or {}
        if reason in ("TRACKING_STALLED", "TRACKING_RESHIP_REVIEW") and not wh.get("ascan_time") \
                and (not trk.get("events") or trk.get("status") == "InfoReceived"):
            wanted = ("无Ascan",) + wanted  # 没有轨迹或只有面单信息、谷仓也没有揽收时间：先查有没有揽收扫描
        picked = _pick_type(client, wh.get("shipping_method"), wanted)
        if not picked:
            out.append({"tracking_no": tn, "skipped": f"物流方式 {wh.get('shipping_method')} 不支持所需查件类型"})
            continue
        desc = _desc(reason, order_id, pkg)
        io_code = DRAFT_PREFIX + uuid.uuid4().hex[:12].upper()  # 只存本系统；提交到谷仓后换成谷仓查件单号
        now = utcnow()
        db.add(LogisticsTicket(io_code=io_code, order_code=wh["order_code"], reference_no=order_id, tracking_no=tn,
                               type_name=picked[1], it_type_id=picked[0], sm_code=wh.get("shipping_method"),
                               io_status_text=DRAFT_TEXT, warehouse_code=wh.get("warehouse_code"),
                               store_id=store_id, buyer_key=buyer_key, conversation_id=conversation_id,
                               origin="AI_DRAFT", io_desc=desc, created_by=ACTOR, created_at=now, synced_at=now))
        db.add(AuditEvent(actor="agent", action="LOGISTICS_TICKET_DRAFTED", store_id=store_id,
                          conversation_id=conversation_id,
                          detail={"io_code": io_code, "type": picked[1], "reason": reason, "order_id": order_id}))
        out.append({"tracking_no": tn, "io_code": io_code, "type": picked[1], "reason": reason})
    return out


def auto_submit(db: Session, client: GoodcangClient, tickets: list[dict]) -> None:
    """LOGISTICS_TICKET_AUTO_SUBMIT：刚起草的查件单直接提交到谷仓。提交失败的留作草稿，客服在工作台处理。"""
    for item in tickets:
        if not item.get("io_code"):
            continue
        t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == item["io_code"]))
        try:
            submit_draft(db, t, client, ACTOR)
            item["io_code"], item["submitted"] = t.io_code, True
        except (GoodcangError, TicketError) as e:
            item["submit_error"] = str(e)[:200]


def auto_handled(follow: dict | None) -> set[str]:
    """已经自动提交了查件单（或已有未完结的查件单在跟）的物流原因：这些不用再因物流原因转人工。"""
    if not follow or not get_settings().logistics_ticket_auto_submit:
        return set()
    return {t["reason"] for t in follow.get("tickets") or [] if t.get("reason")
            and (t.get("submitted") or t.get("skipped") == OPEN_SKIP)}


# ---------------------------------------------------------------- 补发库存
def reship_stock(client: GoodcangClient, order_id: str, fallback_items: list[dict] | None = None) -> dict:
    """订单商品在原发货区域仓的可售库存。优先用谷仓出库单里的商品 SKU，没有出库单时用 eBay 订单 SKU 换成谷仓 SKU。"""
    whs = client.orders_by_reference(order_id)
    region = next((w.get("warehouse_code") for w in whs if w.get("warehouse_code")), None)
    items = [i for w in whs for i in w.get("items") or []] or fallback_items or []
    need: dict[str, int] = {}
    for i in items:
        sku = warehouse_sku(i.get("sku"))
        if sku:
            need[sku] = need.get(sku, 0) + int(i.get("quantity") or 1)
    if not need:
        return {"order_id": order_id, "items": [], "summary": "查不到订单商品 SKU，补发前请人工核对库存"}
    stock = client.inventory(list(need))
    rows = []
    for sku, qty in need.items():
        mine = [r for r in stock if (r["sku"] or "").upper() == sku.upper()]
        here = next((r for r in mine if region and r["warehouse_code"] == region), None)
        others = [f"{r['warehouse_desc'] or r['warehouse_code']} {r['sellable']}" for r in mine
                  if r is not here and r["sellable"] > 0]
        rows.append({"sku": sku, "quantity": qty, "warehouse_code": region,
                     "sellable": here["sellable"] if here else 0, "enough": bool(here and here["sellable"] >= qty),
                     "other_warehouses": others})
    ok = all(r["enough"] for r in rows)
    parts = [f"{r['sku']} {r['warehouse_code'] or '原仓'}可售 {r['sellable']}（需 {r['quantity']}）"
             + (f"，其他仓：{'、'.join(r['other_warehouses'])}" if r["other_warehouses"] else "") for r in rows]
    verdict = "有库存，可以补发" if ok else "原发货仓库存不足，不能补发，只能协商退款"
    return {"order_id": order_id, "warehouse_code": region, "items": rows, "all_in_stock": ok,
            "summary": f"补发库存：{'；'.join(parts)} → {verdict}"}


# ---------------------------------------------------------------- 入口
def after_reply(db: Session, job, conv, state, reasons: list[str], client: GoodcangClient | None = None) -> dict | None:
    """回复流程落库时调用：按需建查件单草稿、查补发库存。没有相关原因或谷仓没配置时返回 None。"""
    s = get_settings()
    want_ticket = s.logistics_ticket_auto_draft and any(r in TICKET_TYPES for r in reasons)
    want_stock = any(r in RESHIP_REASONS for r in reasons)
    if not (want_ticket or want_stock) or not s.goodcang_ready:
        return None
    client = client or GoodcangClient(s)
    out: dict = {}
    if want_ticket:
        try:
            out["tickets"] = draft_tickets(db, client, state, reasons, store_id=conv.store_id if conv else None,
                                           buyer_key=conv.buyer_key if conv else None,
                                           conversation_id=conv.id if conv else None)
            if s.logistics_ticket_auto_submit and conv is not None and conv.channel_source is not None:
                db.flush()
                auto_submit(db, client, out["tickets"])
            elif s.logistics_ticket_auto_submit and any(t.get("io_code") for t in out["tickets"]):
                out["submit_skipped"] = "测试会话：只起草不提交到谷仓"  # 测试会话用的是真实订单号，不能真的去谷仓建单
        except GoodcangError as e:
            out["ticket_error"] = str(e)
    if want_stock:
        order = _order(state)
        order_id = (order or {}).get("order_id") or next((p.get("order_id") for p in _packages(state) if p.get("order_id")), None)
        if order_id:
            try:
                out["stock"] = reship_stock(client, order_id, (order or {}).get("items"))
            except GoodcangError as e:
                out["stock_error"] = str(e)
    append_note(conv, out)
    return out


def append_note(conv, out: dict | None) -> None:
    """把补发库存、查件单结果追加到 AI 标的“待售后处理”备注里（会话还没被 AI 标售后时不动）。
    回复流程里先跑 after_reply 再标售后，所以标完售后要再调一次，否则第一次标的备注里没有库存和查件单。"""
    if not out or conv is None or conv.aftersales_marked_by != ACTOR or conv.aftersales_status != Aftersales.PENDING:
        return
    tickets = out.get("tickets") or []
    sub = [f"{t['type']} {t['io_code']}" for t in tickets if t.get("submitted")]
    new = [t["type"] for t in tickets if t.get("io_code") and not t.get("submitted")]
    drafts = [t["existing"] for t in tickets if t.get("skipped") == DRAFT_SKIP and t.get("existing")]
    stale = [f"{t['existing']}（{t.get('age_days')} 天）" for t in tickets if t.get("skipped") == STALE_SKIP]
    elsewhere = [t["tracking_no"] for t in tickets if t.get("skipped") == "不是谷仓发的货或缺跟踪号" and t.get("tracking_no")]
    extra = [x for x in (
        (out.get("stock") or {}).get("summary"),
        f"已自动向谷仓提交查件单（{'、'.join(sub)}）" if sub else None,
        f"已起草查件单（{'、'.join(new)}），请在“查件单”页核对后提交到谷仓" if new else None,
        f"已有查件单草稿 {'、'.join(drafts)} 还没提交到谷仓，请在“查件单”页核对后提交" if drafts else None,
        f"查件单 {'、'.join(stale)} 跟进已超时、问题还在，请判断补发或再次催查" if stale else None,
        f"{'、'.join(elsewhere)} 不是谷仓发的货：系统不能建查件单、查不到补发库存，请到对应仓库/承运商处理" if elsewhere else None,
        out.get("submit_skipped")) if x]
    extra = [x for x in extra if x not in (conv.aftersales_note or "")]
    if extra:
        conv.aftersales_note = "；".join([conv.aftersales_note or ""] + extra).strip("；")[:1000]


# ---------------------------------------------------------------- 工作台：修改、提交、废弃草稿
class TicketError(ValueError):
    pass


def edit_draft(t: LogisticsTicket, *, it_type_id: int | None = None, type_name: str | None = None,
               io_desc: str | None = None) -> None:
    if not is_local_draft(t):
        raise TicketError("只有本系统还没提交的草稿可以修改")
    if it_type_id is not None:
        t.it_type_id, t.type_name = int(it_type_id), (type_name or t.type_name)
    if io_desc is not None:
        desc = io_desc.strip()
        if not 5 <= len(desc) <= 300:
            raise TicketError("查件需求要 5–300 个字")
        t.io_desc = desc


def submit_draft(db: Session, t: LogisticsTicket, client: GoodcangClient, actor: str) -> LogisticsTicket:
    """把本系统的草稿提交到谷仓（save_type=2），查件单号换成谷仓返回的；之后每天同步谷仓状态和回复。"""
    if not is_local_draft(t):
        raise TicketError("这张查件单不是本系统的草稿，或已经提交过")
    if not (t.order_code and t.reference_no and t.tracking_no and t.it_type_id and t.io_desc):
        raise TicketError("草稿缺少出库单号、订单号、跟踪号、查件类型或查件需求")
    draft_code = t.io_code
    io_code = client.create_ticket(order_code=t.order_code, reference_no=t.reference_no, tracking_no=t.tracking_no,
                                   it_type_id=t.it_type_id, io_desc=t.io_desc, emails=get_settings().logistics_ticket_emails)
    now = utcnow()
    t.io_code, t.io_status_text, t.submitted_by, t.submitted_at, t.synced_at = io_code, SUBMITTED_TEXT, actor, now, now
    db.add(AuditEvent(actor=actor, action="LOGISTICS_TICKET_SUBMITTED", store_id=t.store_id, conversation_id=t.conversation_id,
                      detail={"io_code": io_code, "draft": draft_code, "type": t.type_name, "order_id": t.reference_no}))
    return t


def discard_draft(db: Session, t: LogisticsTicket, actor: str) -> LogisticsTicket:
    if not is_local_draft(t):
        raise TicketError("只有本系统还没提交的草稿可以废弃；已提交的请在谷仓后台处理")
    now = utcnow()
    t.io_status, t.io_status_text, t.handled_at, t.handled_by = 10, DISCARDED_TEXT, now, actor
    db.add(AuditEvent(actor=actor, action="LOGISTICS_TICKET_DISCARDED", store_id=t.store_id, conversation_id=t.conversation_id,
                      detail={"io_code": t.io_code, "order_id": t.reference_no}))
    return t


# ---------------------------------------------------------------- 提交后安抚买家
NOTICE_ACTOR = "AI（查件安抚）"
# 买家这次只问了物流（或顺带致谢）才发固定的安抚消息；还问了别的，交给人工一起回
NOTICE_INTENTS = {"shipping_status", "delivery_delay", "handling_time", "thanks_closing"}
# 这些原因不影响发安抚消息（安抚消息是固定话术，不涉及承诺）；其他原因（合规、纠纷、退款、发票等）留给人工
NOTICE_OK_REASONS = {"MODEL_REQUESTED_HUMAN", "RISK_NOT_L0", "INTENT_NOT_WHITELISTED", "STORE_AUTO_SEND_OFF",
                     "AUTO_SEND_DISABLED", "NO_KB_EVIDENCE", "AUTO_REPLY_STREAK", "TRACKING_STALLED",
                     "TRACKING_RESHIP_REVIEW", "TRACKING_EXCEPTION", "TRACKING_NOT_FOUND",
                     "TRACKING_DELIVERED_NOT_RECEIVED", "TRACKING_LOST_DAMAGED", "RESHIP_REQUESTED"}
NOTICE_TEXT = {
    "en": "Thank you for your message and your patience. We have passed your request on to our logistics team, and they "
          "have opened an inquiry with the carrier about your parcel. As soon as we receive the carrier's reply, we will "
          "let you know right away.",
    "de": "Vielen Dank für Ihre Nachricht und Ihre Geduld. Wir haben Ihr Anliegen an unser Logistik-Team weitergegeben, "
          "das bereits eine Nachforschung beim Versanddienstleister zu Ihrem Paket eingeleitet hat. Sobald uns die "
          "Rückmeldung des Versanddienstleisters vorliegt, informieren wir Sie umgehend.",
    "fr": "Merci pour votre message et votre patience. Nous avons transmis votre demande à notre équipe logistique, qui a "
          "ouvert une enquête auprès du transporteur concernant votre colis. Dès que nous aurons la réponse du "
          "transporteur, nous vous en informerons immédiatement.",
    "it": "Grazie per il messaggio e per la pazienza. Abbiamo inoltrato la sua richiesta al nostro team logistico, che ha "
          "aperto una verifica con il corriere sul suo pacco. Appena riceveremo la risposta del corriere, la informeremo "
          "subito.",
    "es": "Gracias por su mensaje y su paciencia. Hemos trasladado su consulta a nuestro equipo de logística, que ha "
          "abierto una investigación con el transportista sobre su paquete. En cuanto recibamos la respuesta del "
          "transportista, le informaremos de inmediato.",
}
NOTICE_ZH = "感谢您的来信和耐心等待。我们已把您的问题转给物流同事，他们已就您的包裹向承运商发起查询。一收到承运商的回复，我们会马上通知您。"
_SITE_LANG = {"EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de", "EBAY_FR": "fr", "EBAY_BE": "fr", "EBAY_IT": "it",
              "EBAY_ES": "es"}


def _notice_language(db: Session, conv, hint: str | None) -> str:
    """买家语言：本次 AI 草稿的语言 → 这个会话最近一次 AI 草稿的语言 → 站点语言 → 英语。"""
    from ..models import Draft, ReplyJob

    lang = (hint or "").lower()[:2]
    if lang not in NOTICE_TEXT:
        last = db.scalar(select(Draft.language).join(ReplyJob, Draft.job_id == ReplyJob.id)
                         .where(ReplyJob.conversation_id == conv.id, Draft.author == "agent", Draft.language.is_not(None))
                         .order_by(Draft.id.desc()).limit(1))
        lang = (last or "").lower()[:2]
    return lang if lang in NOTICE_TEXT else _SITE_LANG.get(conv.marketplace_id or "", "en")


def send_submit_notice(db: Session, tickets: list[LogisticsTicket], *, respect_claim: bool = False,
                       approved_by: str | None = None) -> str:
    """查件单提交到谷仓后，给买家发一条固定的安抚消息（模拟发送）：已转物流同事、已向承运商查询、有结果马上通知。
    会话里有待审的 AI 草稿时一并结案（这条就是本次回复）。买家还问了别的、有其他需人工的原因、客服正在写回复时不发，
    留给人工。返回 SENT 或没发的原因（同时记在查件单上）。谷仓回复后由查件单同步再把结果发给买家。

    approved_by：客服在工作台提交查件单时勾了“通知买家”——这就是人工审核过的发送，不看 LOGISTICS_TICKET_SUBMIT_NOTICE
    （那个开关只管 AI 自动提交后的自动通知），“同事在跟进”也不挡（跟进的同事就是提交的人）。"""
    from ..models import Conversation, Draft, JobState, ReplyJob, WorkClaim
    from ..sending.service import GENERATING, SendError, simulate_send

    tickets = [t for t in tickets if t is not None and t.notice_sent_at is None]
    if not tickets:
        return "已发过"

    def skip(reason: str) -> str:
        for t in tickets:
            t.notice_status = reason[:200]
        return reason

    if not approved_by and not get_settings().logistics_ticket_submit_notice:
        return skip("未开启 LOGISTICS_TICKET_SUBMIT_NOTICE")
    conv_id = next((t.conversation_id for t in tickets if t.conversation_id), None)
    conv = db.get(Conversation, conv_id) if conv_id else None
    if conv is None:
        return skip("没有买家会话，等谷仓回复后再通知")
    if respect_claim:
        claim = db.scalar(select(WorkClaim).where(WorkClaim.store_id == conv.store_id, WorkClaim.conversation_id == conv.id))
        if claim and claim.expires_at > utcnow():
            return skip("有同事正在处理这个会话，没发")
    job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == conv.id, ReplyJob.state != JobState.CANCELLED_STALE)
                    .order_by(ReplyJob.id.desc()).limit(1))
    draft = None
    if job is not None and job.state in GENERATING:
        return skip("AI 正在生成回复，没发")
    if job is not None and job.state in JobState.PENDING_REVIEW:
        draft = db.scalar(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE").order_by(Draft.id.desc()).limit(1))
        if draft is not None and draft.author != "agent":
            return skip("客服正在写回复，没发")
        other = set((draft.intents if draft else None) or []) - NOTICE_INTENTS
        if other:
            return skip("买家还问了其他问题（" + "、".join(sorted(other)) + "），留给人工一起回复")
        blocking = set(job.decision_reasons or []) - NOTICE_OK_REASONS - ({"HUMAN_FOLLOWING"} if approved_by else set())
        if blocking:
            return skip("还有其他需人工的原因（" + "、".join(sorted(blocking)) + "），留给人工回复")
    lang = _notice_language(db, conv, draft.language if draft else None)
    try:
        with db.begin_nested():
            msg, _ = simulate_send(db, conv=conv, text=NOTICE_TEXT[lang], actor=NOTICE_ACTOR, text_zh=NOTICE_ZH, auto=True)
            msg.source, msg.meta = "logistics_notice", {**(msg.meta or {}), "ticket_ids": [t.id for t in tickets]}
    except SendError as e:
        return skip(f"没发：{e.message}")
    now = utcnow()
    for t in tickets:
        t.notice_status, t.notice_sent_at = "SENT", now
    db.add(AuditEvent(actor=NOTICE_ACTOR, action="LOGISTICS_TICKET_NOTICE_SENT", store_id=conv.store_id,
                      conversation_id=conv.id, detail={"io_codes": [t.io_code for t in tickets], "language": lang,
                                                       "outbound_id": msg.id,
                                                       **({"approved_by": approved_by} if approved_by else {})}))
    return "SENT"
