"""Best offer：买家在刊登上出的报价（Trading GetBestOffers 拉取）与卖家回应（RespondToBestOffer：接受 / 还价 / 拒绝）。

- 拉取只读：全部有秘钥的 eBay 店铺，每 BEST_OFFER_SYNC_MINUTES 分钟（报价 48 小时过期；eBay 的 OFFER_ACTIVITY 推送要 sell.offer 权限，
  现有授权没有，所以靠定时拉）；eBay 只返回还在处理中的报价，之前有、这次没有的按过期时间或回应结果标记；
- 回应是真实写操作：只有对接 eBay 的店铺（EBAY_LIVE_STORES）能在系统里操作；接受后买家即成交，页面二次确认；
- 不存买家邮箱和地址（GetBestOffers 会返回，只取账号和信用分）。
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import AuditEvent, BestOffer, Store

log = logging.getLogger(__name__)
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
TRADING_URL = "https://api.ebay.com/ws/api.dll"
OPEN = ("Pending", "Active")
TYPE_LABELS = {"BuyerBestOffer": "买家报价", "BuyerCounterOffer": "买家还价", "SellerCounterOffer": "我方还价"}
STATUS_LABELS = {"Pending": "待处理", "Active": "待处理", "Accepted": "已接受", "Declined": "已拒绝", "Countered": "已还价",
                 "Expired": "已过期", "Retracted": "买家已撤回", "Gone": "已结束（eBay 不再返回）"}


def _dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def parse(root: ET.Element) -> list[dict]:
    out = []
    for group in root.findall("e:ItemBestOffersArray/e:ItemBestOffers", NS):
        item = group.find("e:Item", NS)
        item_id = item.findtext("e:ItemID", namespaces=NS) if item is not None else None
        bin_el = item.find("e:BuyItNowPrice", NS) if item is not None else None
        for o in group.findall("e:BestOfferArray/e:BestOffer", NS):
            price = o.find("e:Price", NS)
            out.append({
                "best_offer_id": o.findtext("e:BestOfferID", namespaces=NS), "item_id": item_id,
                "bin_price": float(bin_el.text) if bin_el is not None and bin_el.text else None,
                "currency": price.get("currencyID") if price is not None else (bin_el.get("currencyID") if bin_el is not None else None),
                "price": float(price.text) if price is not None and price.text else None,
                "quantity": int(o.findtext("e:Quantity", namespaces=NS) or 1),
                "buyer": (o.findtext("e:Buyer/e:UserID", namespaces=NS) or "").lower() or None,
                "buyer_feedback": int(o.findtext("e:Buyer/e:FeedbackScore", namespaces=NS) or 0),
                "status": o.findtext("e:Status", namespaces=NS), "offer_type": o.findtext("e:BestOfferCodeType", namespaces=NS),
                "buyer_message": o.findtext("e:BuyerMessage", namespaces=NS), "seller_message": o.findtext("e:SellerMessage", namespaces=NS),
                "expires_at": _dt(o.findtext("e:ExpirationTime", namespaces=NS)),
                "item_end_at": _dt(item.findtext("e:ListingDetails/e:EndTime", namespaces=NS)) if item is not None else None,
            })
    return out


def fetch_store(store_id: str, marketplace: str | None) -> list[dict]:
    from .sources.ebay import EbayReadOnlyApi

    api = EbayReadOnlyApi(store_id)
    rows: list[dict] = []
    for page in range(1, 6):
        root = api.trading("GetBestOffers", "<DetailLevel>ReturnAll</DetailLevel>"
                                            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>",
                           marketplace)
        if root.findtext("e:Ack", namespaces=NS) == "Failure":
            raise RuntimeError(root.findtext("e:Errors/e:LongMessage", namespaces=NS) or "GetBestOffers 失败")
        rows += parse(root)
        pages = int(root.findtext("e:PaginationResult/e:TotalNumberOfPages", namespaces=NS) or 0)
        if page >= pages:
            break
    return rows


def save(db: Session, store_id: str, rows: list[dict]) -> dict:
    """按报价号更新；之前在处理中、这次 eBay 不再返回的，过期了记已过期，否则记已结束（可能在 eBay 后台处理了）。"""
    now = utcnow()
    existing = {o.best_offer_id: o for o in db.scalars(select(BestOffer).where(BestOffer.store_id == store_id))}
    seen, new = set(), 0
    for r in rows:
        if not r.get("best_offer_id"):
            continue
        seen.add(r["best_offer_id"])
        o = existing.get(r["best_offer_id"])
        if o is None:
            o = BestOffer(store_id=store_id, best_offer_id=r["best_offer_id"], first_seen_at=now)
            db.add(o)
            new += 1
        for k, v in r.items():
            if k != "best_offer_id":
                setattr(o, k, v)
        o.last_seen_at = now
    gone = 0
    for bid, o in existing.items():
        if bid not in seen and o.status in OPEN:
            o.status = "Expired" if o.expires_at and o.expires_at < now else "Gone"
            gone += 1
    db.flush()
    return {"offers": len(rows), "new": new, "gone": gone}


def fill_titles(db: Session, store_id: str) -> None:
    """新报价补商品标题（GetItem，有缓存），每次最多 20 个商品。"""
    from .sources import get_source_for_store

    missing = db.scalars(select(BestOffer).where(BestOffer.store_id == store_id, BestOffer.title.is_(None),
                                                 BestOffer.item_id.is_not(None)).limit(50)).all()
    items = list(dict.fromkeys(o.item_id for o in missing))[:20]
    if not items:
        return
    src = get_source_for_store(store_id)
    info = {}
    for iid in items:
        try:
            card = src.get_listing(store_id, iid) or {}
            info[iid] = (card.get("title"), card.get("picture_url"))
        except Exception:  # noqa: BLE001
            info[iid] = (None, None)
    for o in missing:
        t, pic = info.get(o.item_id, (None, None))
        o.title, o.picture_url = (t or "")[:300] or None, pic


def sync_all() -> dict:
    from .security.store_credentials import ebay_store_rows

    with session_scope() as db:
        stores = [(c.store_id, (db.get(Store, c.store_id).default_marketplace_id if db.get(Store, c.store_id) else None))
                  for c in ebay_store_rows(db)]
    stats = {"stores": len(stores), "offers": 0, "new": 0, "errors": {}}
    for sid, mk in stores:
        try:
            rows = fetch_store(sid, mk)
            with session_scope() as db:
                r = save(db, sid, rows)
                fill_titles(db, sid)
            stats["offers"] += r["offers"]
            stats["new"] += r["new"]
        except Exception as e:  # noqa: BLE001 — 一个店铺失败不影响其他店铺
            stats["errors"][sid] = str(e)[:200]
    with session_scope() as db:
        db.add(AuditEvent(actor="best_offers", action="BEST_OFFER_SYNC", detail=stats))
    return stats


# ------------------------------------------------------------------ 回应（真实写操作）
class OfferError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status, message


def can_respond(store: Store | None) -> str | None:
    """不能在系统里回应的原因；None = 可以。"""
    from .sending.channel import is_live_store

    if get_settings().ebay_channel != "ebay":
        return "发送渠道不是 eBay（EBAY_CHANNEL=mock），不能回应报价"
    if not is_live_store(store):
        return "这个店铺没有对接 eBay（不在“对接 eBay 的店铺”里），请到 eBay 后台处理"
    return None


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def respond(db: Session, offer: BestOffer, action: str, actor: str, *, price: float | None = None,
            quantity: int | None = None, message: str | None = None, http: httpx.Client | None = None) -> BestOffer:
    """Accept 接受 / Counter 还价（必须给价格）/ Decline 拒绝。"""
    from .ebay.auth import for_store
    from .sources.ebay import MARKETPLACE_SITE_ID

    if action not in ("Accept", "Counter", "Decline"):
        raise OfferError(422, "只能接受、还价或拒绝")
    if offer.status not in OPEN:
        raise OfferError(409, f"这条报价已经{STATUS_LABELS.get(offer.status, offer.status)}，不能再操作")
    if offer.expires_at and offer.expires_at < utcnow():
        raise OfferError(409, "这条报价已经过期")
    store = db.get(Store, offer.store_id)
    reason = can_respond(store)
    if reason:
        raise OfferError(403, reason)
    if action == "Counter" and (not price or price <= 0):
        raise OfferError(422, "还价要填价格")
    body = f"<Action>{action}</Action><BestOfferID>{_xml(offer.best_offer_id)}</BestOfferID><ItemID>{_xml(offer.item_id or '')}</ItemID>"
    if action == "Counter":
        body += f"<CounterOfferPrice currencyID=\"{_xml(offer.currency or 'USD')}\">{price:.2f}</CounterOfferPrice>"
        body += f"<CounterOfferQuantity>{int(quantity or offer.quantity or 1)}</CounterOfferQuantity>"
    if message and message.strip():
        body += f"<SellerResponse>{_xml(message.strip()[:250])}</SellerResponse>"
    xml = (f"<?xml version='1.0' encoding='utf-8'?><RespondToBestOfferRequest xmlns='urn:ebay:apis:eBLBaseComponents'>"
           f"{body}</RespondToBestOfferRequest>")
    try:
        resp = (http or httpx.Client(timeout=30)).post(TRADING_URL, content=xml.encode("utf-8"), headers={
            "X-EBAY-API-CALL-NAME": "RespondToBestOffer",
            "X-EBAY-API-SITEID": MARKETPLACE_SITE_ID.get(store.default_marketplace_id or "", "0"),
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1311", "X-EBAY-API-IAF-TOKEN": for_store(offer.store_id).access_token(),
            "Content-Type": "text/xml"})
        root = ET.fromstring(resp.content)
    except (httpx.HTTPError, ET.ParseError) as e:
        offer.response_error = f"请求出错（{type(e).__name__}），请到 eBay 后台确认这条报价有没有处理"[:500]
        db.flush()
        raise OfferError(502, offer.response_error) from None
    ack = root.findtext("e:Ack", namespaces=NS)
    err = root.findtext("e:Errors/e:LongMessage", namespaces=NS) or root.findtext("e:Errors/e:ShortMessage", namespaces=NS)
    if ack not in ("Success", "Warning"):
        offer.response_error = (err or f"eBay 返回 {ack}")[:500]
        db.add(AuditEvent(actor=actor[:64], action="BEST_OFFER_RESPOND_FAILED", store_id=offer.store_id,
                          detail={"best_offer_id": offer.best_offer_id, "action": action, "error": offer.response_error}))
        db.flush()
        raise OfferError(422, offer.response_error)
    offer.status = {"Accept": "Accepted", "Counter": "Countered", "Decline": "Declined"}[action]
    offer.responded_action, offer.responded_by, offer.responded_at = action, actor[:64], utcnow()
    offer.counter_price = price if action == "Counter" else None
    offer.response_message, offer.response_error = (message or "").strip()[:250] or None, None
    db.add(AuditEvent(actor=actor[:64], action="BEST_OFFER_RESPONDED", store_id=offer.store_id,
                      detail={"best_offer_id": offer.best_offer_id, "item_id": offer.item_id, "action": action,
                              "price": price, "buyer_price": offer.price, "currency": offer.currency}))
    db.flush()
    return offer


# ------------------------------------------------------------------ 定时拉取
class BestOfferWorker:
    """每 BEST_OFFER_SYNC_MINUTES 分钟拉一次全部店铺的 Best offer（0 = 暂停，页面上仍可立即刷新）。"""

    def __init__(self):
        import threading

        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.last_at: datetime | None = None
        self.running_now = False

    def start(self) -> bool:
        import threading

        from .ebay.stores import ebay_not_ready_reasons

        if ebay_not_ready_reasons():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="best-offers", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_now(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        self.running_now = True
        try:
            self.last = sync_all()
        except Exception as e:  # noqa: BLE001
            log.exception("Best offer 拉取失败")
            self.last = {"error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self.last_at = utcnow()
            self.running_now = False
            self._lock.release()
        return self.last

    def run_in_background(self) -> bool:
        import threading

        if self.running_now:
            return False
        threading.Thread(target=self.run_now, name="best-offers-now", daemon=True).start()
        return True

    def _loop(self) -> None:
        from datetime import timedelta

        self._stop.wait(45)
        while not self._stop.is_set():
            minutes = get_settings().best_offer_sync_minutes
            if minutes > 0 and (self.last_at is None or utcnow() - self.last_at >= timedelta(minutes=minutes)):
                self.run_now()
            self._stop.wait(60)


worker = BestOfferWorker()
