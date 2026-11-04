"""发货通知：每天汇总所有店铺当天在 eBay 标记已发货的订单，按站点语言写好订单号、承运商、跟踪号和承运商官网（买家自己在官网输入单号查），
客服在工作台“发货通知”页审核（可修改、跳过）后发给买家；买家已有会话就发在会话里，没有就按订单新建。

- 只读 eBay：订单列表（按最近修改时间 + 已发货状态过滤）和每单的发货记录（shipping_fulfillment）。
- 当天 = 北京时间；按 eBay 记录的标记发货时间算。承运商以 eBay 为准（买家在订单里看到的就是它）。
- 一个订单只生成一次；有取消申请、有退款的订单直接标“不发”。消息不写任何送达日期。
- SHIP_NOTICE_WAIT_FOR_WAREHOUSE：eBay 标记发货时仓库多半只是打好面单，等谷仓出库单变成“已发货”（或有揽收扫描）
  才生成，买家拿到单号就能查到；没出库的下次汇总再看（eBay 标记发货后 SHIP_NOTICE_LOOKBACK_DAYS 天内都会再看），
  通知算在仓库实际发出的那天。谷仓查不到出库单的（不是谷仓发的货）不等。
- 目前为模拟发送：只在本系统会话里追加一条店铺消息，不调用 eBay。
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import AuditEvent, Conversation, ShipNotice, Store

log = logging.getLogger(__name__)
CN = timezone(timedelta(hours=8))
MAX_PAGES = 10

# ---------------------------------------------------------------- 承运商 → 显示名、官网
# 只给官网，买家自己在官网输入跟踪号查询。按 eBay 的承运商名称匹配（不区分大小写，先具体后笼统）；
# 匹配不到的查 17TRACK 运输商表（data/track17_carriers.json）里的官网，再没有就给 17TRACK 首页。
CARRIERS: list[tuple[str, str, str]] = [
    (r"dhl\s*express", "DHL Express", "https://www.dhl.com/"),
    (r"dhl|deutsche\s*post", "DHL", "https://www.dhl.de/"),
    (r"royal\s*mail", "Royal Mail", "https://www.royalmail.com/"),
    (r"parcelforce", "Parcelforce", "https://www.parcelforce.com/"),
    (r"evri|hermes.*uk|myhermes.*uk", "Evri", "https://www.evri.com/"),
    (r"hermes", "Hermes", "https://www.myhermes.de/"),
    (r"dpd.*uk", "DPD", "https://www.dpd.co.uk/"),
    (r"dpd", "DPD", "https://www.dpd.com/de/"),
    (r"gls", "GLS", "https://gls-group.com/"),
    (r"yodel", "Yodel", "https://www.yodel.co.uk/"),
    (r"inpost", "InPost", "https://inpost.co.uk/"),
    (r"ups", "UPS", "https://www.ups.com/"),
    (r"fedex", "FedEx", "https://www.fedex.com/"),
    (r"usps", "USPS", "https://www.usps.com/"),
    (r"yun|云途", "YunExpress", "https://www.yuntrack.com/"),
]
FALLBACK_URL = "https://www.17track.net/"


def _track17_site(name: str) -> str | None:
    """17TRACK 运输商表（只读本地缓存，不联网）里同名承运商的官网。"""
    import json

    from .logistics.track17 import CARRIER_CACHE

    try:
        for c in json.loads(CARRIER_CACHE.read_text(encoding="utf-8")):
            if (c.get("_name") or "").strip().lower() == name.lower() and c.get("_url"):
                return c["_url"]
    except Exception:  # noqa: BLE001  运输商表读不到时用 17TRACK 首页
        return None
    return None


def carrier_link(carrier: str | None, tracking_no: str | None = None, marketplace_id: str | None = None) -> tuple[str, str]:
    """返回（承运商显示名, 官网）。英国站的 DPD、Hermes 用英国官网。跟踪号不拼进网址。"""
    name = (carrier or "").strip()
    probe = name.lower()
    if marketplace_id == "EBAY_GB" and re.search(r"^dpd|hermes", probe):
        probe += " uk"
    for pattern, display, url in CARRIERS:
        if re.search(pattern, probe):
            return display, url
    return name or "Carrier", (_track17_site(name) if name else None) or FALLBACK_URL


# ---------------------------------------------------------------- 消息模板（不写送达日期）
LANG_BY_SITE = {"EBAY_DE": "de", "EBAY_AT": "de", "EBAY_CH": "de", "EBAY_FR": "fr", "EBAY_IT": "it", "EBAY_ES": "es"}
TEMPLATES = {
    "de": ("Hallo,\n\nvielen Dank für Ihre Bestellung {order_id}. Ihr Paket wurde versandt:\n{lines}\n\n"
           "Den aktuellen Stand können Sie jederzeit auf der Website des Paketdienstes abfragen, "
           "indem Sie dort die Sendungsnummer eingeben.\n\nMit freundlichen Grüßen",
           "- {carrier}, Sendungsnummer: {tracking_no}, Website: {url}"),
    "en": ("Hello,\n\nThank you for your order {order_id}. Your parcel has been shipped:\n{lines}\n\n"
           "You can check the latest status at any time by entering the tracking number on the carrier's website."
           "\n\nBest regards",
           "- {carrier}, tracking number: {tracking_no}, website: {url}"),
    "fr": ("Bonjour,\n\nMerci pour votre commande {order_id}. Votre colis a été expédié :\n{lines}\n\n"
           "Vous pouvez suivre votre envoi à tout moment en saisissant le numéro de suivi sur le site du transporteur."
           "\n\nCordialement",
           "- {carrier}, numéro de suivi : {tracking_no}, site : {url}"),
    "it": ("Buongiorno,\n\ngrazie per il suo ordine {order_id}. Il pacco è stato spedito:\n{lines}\n\n"
           "Può verificare lo stato della spedizione in qualsiasi momento inserendo il numero di tracciamento "
           "sul sito del corriere.\n\nCordiali saluti",
           "- {carrier}, numero di tracciamento: {tracking_no}, sito: {url}"),
    "es": ("Hola,\n\ngracias por su pedido {order_id}. Su paquete ha sido enviado:\n{lines}\n\n"
           "Puede consultar el estado del envío en cualquier momento introduciendo el número de seguimiento "
           "en la web de la empresa de transporte.\n\nSaludos cordiales",
           "- {carrier}, número de seguimiento: {tracking_no}, web: {url}"),
}
TEMPLATE_ZH = ("您好，感谢您的订单 {order_id}。您的包裹已发出：\n{lines}\n\n可随时在承运商官网输入跟踪号查看最新状态。",
               "- {carrier}，跟踪号：{tracking_no}，官网：{url}")


def render(order_id: str, packages: list[dict], language: str) -> tuple[str, str]:
    body, line = TEMPLATES.get(language, TEMPLATES["en"])

    def fill(b: str, ln: str) -> str:
        return b.format(order_id=order_id, lines="\n".join(ln.format(**p) for p in packages))

    return fill(body, line), fill(*TEMPLATE_ZH)


# ---------------------------------------------------------------- 汇总当天已发货的订单
def day_range(day: date) -> tuple[datetime, datetime]:
    """北京时间某一天 → UTC 起止（naive）。"""
    start = datetime.combine(day, dtime.min, CN).astimezone(timezone.utc).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _skip_reason(o: dict) -> str | None:
    cancel = o.get("cancelStatus") or {}
    if cancel.get("cancelRequests") or cancel.get("cancelState") not in (None, "NONE_REQUESTED"):
        return "订单有取消申请"
    if (o.get("paymentSummary") or {}).get("refunds"):
        return "订单有退款"
    return None


def _ebay_stores(db: Session) -> list[Store]:
    from .ebay.stores import is_ebay_store

    return [s for s in db.scalars(select(Store).where(Store.ebay_user_id.is_not(None)).order_by(Store.name))
            if is_ebay_store(s.id)]


def _warehouse_shipped(gc, order_id: str, cache: dict) -> bool:
    """谷仓出库单已发货（状态 D）或已有揽收扫描；查不到出库单（不是谷仓发的货）也算可以发通知。"""
    if order_id not in cache:
        try:
            whs = gc.orders_by_reference(order_id)
        except Exception:  # noqa: BLE001  谷仓查不了：这次先不生成，下次再看
            return False
        cache[order_id] = not whs or any(w.get("order_status") == "D" or w.get("ascan_time") for w in whs)
    return cache[order_id]


def collect_store(db: Session, api, store: Store, day: date, gc=None) -> dict:
    s = get_settings()
    start, end = day_range(day)
    wait = s.ship_notice_wait_for_warehouse and gc is not None
    # 等仓库实际发出时：eBay 前几天标记发货、今天才出库的也算今天
    look_from = start - timedelta(days=s.ship_notice_lookback_days) if wait else start
    since = look_from.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    stats = {"orders": 0, "new": 0, "skipped": 0, "waiting": 0}
    wh_cache: dict = {}
    known = set(db.scalars(select(ShipNotice.order_id).where(ShipNotice.store_id == store.id)))
    offset = 0
    for _ in range(MAX_PAGES):
        data = api.get("/sell/fulfillment/v1/order", {
            "filter": f"lastmodifieddate:[{since}..],orderfulfillmentstatus:{{FULFILLED|IN_PROGRESS}}",
            "limit": 200, "offset": offset}) or {}
        page = data.get("orders") or []
        for o in page:
            oid = o.get("orderId")
            if not oid or oid in known or not o.get("fulfillmentHrefs"):
                continue
            fulfil = (api.get(f"/sell/fulfillment/v1/order/{oid}/shipping_fulfillment") or {}).get("fulfillments") or []
            shipped = [(f, _parse(f.get("shippedDate"))) for f in fulfil if f.get("shipmentTrackingNumber")]
            today = [(f, t) for f, t in shipped if t and look_from <= t < end]
            if not today:
                continue  # 不是当天发的（例如只是改了别的字段）
            if wait and not _warehouse_shipped(gc, oid, wh_cache):
                stats["waiting"] += 1  # 仓库还没真正发出：下次汇总再看
                continue
            stats["orders"] += 1
            site = ((o.get("lineItems") or [{}])[0].get("listingMarketplaceId")) or store.default_marketplace_id
            lang = LANG_BY_SITE.get(site, "en")
            pkgs = []
            for f, _t in shipped:
                carrier, url = carrier_link(f.get("shippingCarrierCode"), f["shipmentTrackingNumber"], site)
                pkgs.append({"carrier": carrier, "tracking_no": f["shipmentTrackingNumber"], "url": url,
                             "ebay_carrier": f.get("shippingCarrierCode")})
            text, text_zh = render(oid, pkgs, lang)
            reason = _skip_reason(o)
            db.add(ShipNotice(store_id=store.id, order_id=oid, buyer_key=(o.get("buyer") or {}).get("username"),
                              item_ids=[li.get("legacyItemId") for li in o.get("lineItems") or [] if li.get("legacyItemId")],
                              marketplace_id=site, ship_date=day.isoformat(), shipped_at=min(t for _f, t in today),
                              packages=pkgs, language=lang, text=text, text_zh=text_zh,
                              status="SKIPPED" if reason else "PENDING", skip_reason=reason))
            known.add(oid)
            stats["new"] += 1
            stats["skipped"] += bool(reason)
        offset += len(page)
        if not page or offset >= int(data.get("total") or 0):
            break
    db.flush()
    return stats


def collect(day: date | None = None, *, source=None, store_ids: list[str] | None = None) -> dict:
    """汇总某天（默认北京时间今天）所有店铺已发货的订单。已生成过的订单不重复生成。"""
    from .sources import SourceNotAvailable, get_ebay_source

    day = day or datetime.now(CN).date()
    src = source or get_ebay_source()
    result = {"date": day.isoformat(), "stores": 0, "orders": 0, "new": 0, "skipped": 0, "waiting": 0, "errors": []}
    gc = None
    if get_settings().ship_notice_wait_for_warehouse and get_settings().goodcang_ready:
        from .logistics.goodcang import GoodcangClient

        gc = GoodcangClient()
    with session_scope() as db:
        stores = [s for s in _ebay_stores(db) if not store_ids or s.id in store_ids]
        targets = [(s.id, s.name) for s in stores]
    for sid, name in targets:
        try:
            with session_scope() as db:
                st = collect_store(db, src._api(sid), db.get(Store, sid), day, gc)
            result["stores"] += 1
            for k in ("orders", "new", "skipped", "waiting"):
                result[k] += st[k]
        except SourceNotAvailable as e:
            result["errors"].append(f"{name}：{e}"[:200])
        except Exception as e:  # noqa: BLE001  单个店铺失败不影响其他
            log.exception("发货通知：店铺 %s 汇总失败", sid)
            result["errors"].append(f"{name}：{type(e).__name__}")
    with session_scope() as db:
        db.add(AuditEvent(actor="ship_notices", action="SHIP_NOTICE_COLLECT",
                          detail={k: v for k, v in result.items() if k != "errors"} | {"errors": result["errors"][:20]}))
    return result


# ---------------------------------------------------------------- 发送（模拟）
class NoticeError(ValueError):
    pass


def conversation_for(db: Session, n: ShipNotice, create: bool) -> Conversation | None:
    from .aftersales_mail.service import order_conversation
    from .ingestion.service import get_or_create_conversation

    if n.conversation_id:
        return db.get(Conversation, n.conversation_id)
    cid = order_conversation(db, n.store_id, n.buyer_key, n.order_id, n.item_ids)
    if cid:
        return db.get(Conversation, cid)
    if not create or not n.buyer_key:
        return None
    conv = get_or_create_conversation(db, n.store_id, f"ORDER-{n.order_id}", n.buyer_key, None,
                                      n.marketplace_id or "UNKNOWN", "ship_notice")
    conv.channel_source = conv.channel_source or "logistics"  # 本系统按订单发起的通知会话
    return conv


def send(db: Session, n: ShipNotice, actor: str) -> ShipNotice:
    from .guards.compliance import ComplianceChecker
    from .ingestion.service import _append_message
    from .sending.service import SIMULATED_PREFIX

    if n.status != "PENDING":
        raise NoticeError("这条发货通知已经处理过了")
    text = (n.text or "").strip()
    if not text:
        raise NoticeError("消息内容为空")
    check = ComplianceChecker().check(text)
    if check.result == "BLOCK":
        raise NoticeError("合规检查不通过，请修改后再发送")
    conv = conversation_for(db, n, create=True)
    if conv is None:
        raise NoticeError("找不到订单买家，无法发送")
    msg = _append_message(db, conv, f"{SIMULATED_PREFIX}{uuid.uuid4().hex[:12]}", "SELLER", "OUT", text, None)
    msg.zh_translation = n.text_zh
    conv.row_version += 1
    now = utcnow()
    n.status, n.conversation_id, n.sent_message_id, n.sent_by, n.sent_at = "SENT", conv.id, msg.id, actor, now
    db.add(AuditEvent(actor=actor, action="SIMULATED_SEND", store_id=n.store_id, conversation_id=conv.id,
                      detail={"source": "ship_notice", "order_id": n.order_id, "message_id": msg.ebay_message_id,
                              "compliance": check.result}))
    return n


# ---------------------------------------------------------------- 定时汇总
class ShipNoticeWorker:
    """每隔 SHIP_NOTICE_INTERVAL_MINUTES 分钟汇总一次当天已发货的订单（只生成待审核的通知，不发送）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.last_at: datetime | None = None

    def start(self) -> bool:
        if not get_settings().ship_notice_enabled:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ship-notices", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_now(self, day: date | None = None) -> dict:
        with self._lock:
            self.last = collect(day)
            self.last_at = utcnow()
            return self.last

    def _loop(self) -> None:
        self._stop.wait(90)
        while not self._stop.is_set():
            try:
                self.run_now()
            except Exception:  # noqa: BLE001
                log.exception("发货通知汇总出错")
            self._stop.wait(max(5, get_settings().ship_notice_interval_minutes) * 60)


def counts(db: Session, day: str, store_ids: list[str] | None) -> dict:
    q = select(ShipNotice.status, func.count()).where(ShipNotice.ship_date == day)
    if store_ids is not None:
        q = q.where(ShipNotice.store_id.in_(store_ids))
    return dict(db.execute(q.group_by(ShipNotice.status)).all())


worker = ShipNoticeWorker()
