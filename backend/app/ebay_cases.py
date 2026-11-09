"""售后管理：定时把 eBay 店铺后台的咨询（未收到货）、纠纷、退货、付款纠纷、评价、取消交易、退款单拉到本地（只读）。

- 只调用查询接口：Post-Order return / inquiry / casemanagement / cancellation 的 search，
  Fulfillment payment_dispute_summary（apiz 主机，需 sell.payment.dispute 权限的令牌），
  Finances transaction（REFUND 流水，apiz 主机），Trading GetFeedback；不在 eBay 上做任何操作；
- 全部有秘钥的 eBay 店铺都拉（和消息拉取范围无关），每 EBAY_CASES_SYNC_MINUTES 分钟一次；
- 按（店铺、类型、eBay 编号）去重更新；新出现的记为“新”，状态变了记下变更时间；
- 买家能对上本系统的会话时，列表里可以直接跳到会话。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope, utcnow
from .models import AuditEvent, EbayCase, Store

log = logging.getLogger(__name__)

KINDS = {
    "INQUIRY": "咨询", "CASE": "纠纷", "RETURN": "退货", "PAYMENT_DISPUTE": "付款纠纷",
    "FEEDBACK": "评价管理", "CANCEL": "取消交易", "REFUND": "退款单",
}
PAGE, MAX_PAGES = 100, 5
FEEDBACK_ENTRIES = 200
REFUND_DAYS = 90


# ------------------------------------------------------------------ 解析
def _v(x):
    if isinstance(x, dict):
        return x.get("value") if "value" in x else x.get("formattedValue")
    return x


def _dt(x) -> datetime | None:
    s = _v(x)
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _money(x) -> tuple[float | None, str | None]:
    if not isinstance(x, dict):
        return None, None
    try:
        return float(x.get("value")), x.get("currency")
    except (TypeError, ValueError):
        return None, x.get("currency")


def _closed(*states) -> bool:
    return any(s and ("CLOSED" in str(s).upper() or str(s).upper() in ("RESOLVED", "CANCELLED")) for s in states)


def parse_return(r: dict) -> dict:
    ci = r.get("creationInfo") or {}
    due = r.get("sellerResponseDue") or {}
    amount, cur = _money((r.get("sellerTotalRefund") or {}).get("estimatedRefundAmount"))
    opts = r.get("sellerAvailableOptions") or []
    return {"external_id": str(r.get("returnId")), "order_id": r.get("orderId"), "item_id": str((ci.get("item") or {}).get("itemId") or "") or None,
            "buyer": r.get("buyerLoginName"), "status": f"{r.get('state')}/{r.get('status')}",
            "is_open": not _closed(r.get("state"), r.get("status")), "opened_at": _dt(ci.get("creationDate")),
            "due_at": _dt(due.get("respondByDate")) if due.get("activityDue") else None, "amount": amount, "currency": cur,
            "reason": ci.get("reason"), "comment": (ci.get("comments") or {}).get("content"),
            "detail": {"type": r.get("currentType"), "seller_action": due.get("activityDue"),
                       "action_url": next((o.get("actionURL") for o in opts if o.get("actionURL")), None)}}


def _parse_claim(m: dict, id_key: str, status_key: str) -> dict:
    amount, cur = _money(m.get("claimAmount"))
    ref = f"{m.get('itemId')}-{m.get('transactionId')}" if m.get("itemId") else None
    return {"external_id": str(m.get(id_key)), "order_id": ref, "item_id": str(m.get("itemId") or "") or None,
            "buyer": m.get("buyer"), "status": m.get(status_key), "is_open": not _closed(m.get(status_key)),
            "opened_at": _dt(m.get("creationDate")), "due_at": _dt(m.get("respondByDate")), "amount": amount, "currency": cur,
            "ebay_updated_at": _dt(m.get("lastModifiedDate"))}


def parse_inquiry(m: dict) -> dict:
    return _parse_claim(m, "inquiryId", "inquiryStatusEnum")


def parse_case(m: dict) -> dict:
    return _parse_claim(m, "caseId", "caseStatusEnum")


def parse_cancel(c: dict) -> dict:
    amount, cur = _money(c.get("requestRefundAmount"))
    line = (c.get("lineItems") or [{}])[0]
    return {"external_id": str(c.get("cancelId")), "order_id": c.get("legacyOrderId"), "item_id": line.get("itemId"),
            "buyer": None, "status": f"{c.get('cancelState')}/{c.get('cancelStatus')}",
            "is_open": not _closed(c.get("cancelState"), c.get("cancelStatus")), "opened_at": _dt(c.get("cancelRequestDate")),
            "due_at": _dt(c.get("sellerResponseDueDate")), "amount": amount, "currency": cur, "reason": c.get("cancelReason"),
            "title": line.get("itemTitle"), "ebay_updated_at": _dt(c.get("lastModifiedDate")),
            "detail": {"requestor": c.get("requestorType"), "close_reason": c.get("cancelCloseReason"),
                       "payment_status": c.get("paymentStatus")}}


def parse_dispute(d: dict) -> dict:
    amount, cur = _money(d.get("amount"))
    return {"external_id": str(d.get("paymentDisputeId")), "order_id": d.get("orderId"), "buyer": d.get("buyerUsername"),
            "status": d.get("paymentDisputeStatus"), "is_open": str(d.get("paymentDisputeStatus") or "").upper() != "CLOSED",
            "opened_at": _dt(d.get("openDate")), "due_at": _dt(d.get("respondByDate")), "amount": amount, "currency": cur,
            "reason": d.get("reason"), "detail": {"closed_date": d.get("closedDate")}}


def parse_refund(t: dict) -> dict:
    amount, cur = _money(t.get("amount"))
    return {"external_id": str(t.get("transactionId")), "order_id": t.get("orderId"), "buyer": (t.get("buyer") or {}).get("username"),
            "status": t.get("transactionStatus"), "is_open": False, "opened_at": _dt(t.get("transactionDate")),
            "amount": amount, "currency": cur, "detail": {"booking": t.get("bookingEntry")}}


def parse_feedback(el, ns: dict) -> dict:
    t = lambda p: el.findtext(p, namespaces=ns)  # noqa: E731
    kind = t("e:CommentType")
    responded = bool(el.find("e:FeedbackResponse", ns) is not None and t("e:FeedbackResponse"))
    return {"external_id": t("e:FeedbackID"), "order_id": t("e:OrderLineItemID"), "item_id": t("e:ItemID"),
            "buyer": t("e:CommentingUser"), "status": kind, "rating": kind,
            "is_open": kind in ("Negative", "Neutral") and not responded, "opened_at": _dt(t("e:CommentTime")),
            "title": t("e:ItemTitle"), "comment": t("e:CommentText"), "detail": {"responded": responded}}


# ------------------------------------------------------------------ 拉取
def _apiz_get(tokens, path: str, params: dict, marketplace: str | None) -> dict:
    from .sources import SourceNotAvailable

    base = get_settings().ebay_api_base.replace("://api.", "://apiz.")
    try:
        r = httpx.get(f"{base}{path}", params=params, timeout=30, headers={
            "Authorization": f"Bearer {tokens.access_token()}", "Accept": "application/json",
            **({"X-EBAY-C-MARKETPLACE-ID": marketplace} if marketplace else {})})
    except httpx.HTTPError as e:
        raise SourceNotAvailable(f"连接 eBay 失败：{type(e).__name__}") from None
    if r.status_code == 204:
        return {}
    if r.status_code != 200:
        raise SourceNotAvailable(f"eBay 接口 {path} 返回 HTTP {r.status_code}")
    return r.json()


def _post_order(api, path: str, key: str, mk: str | None) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        d = api.get(path, {"limit": PAGE, "offset": page}, auth="IAF", marketplace=mk) or {}
        items = d.get(key) or []
        rows += items
        pages = (d.get("paginationOutput") or {}).get("totalPages")
        if len(items) < PAGE or (pages and page >= int(pages)):
            break
    return rows


def fetch_store(store_id: str, mk: str | None) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """拉一个店铺全部 7 类。返回 ({类型: [记录]}, {类型: 错误})；某一类失败不影响其他类。"""
    from .ebay.auth import SCOPE_PREFIX, for_store
    from .sources.ebay import NS, EbayReadOnlyApi

    api = EbayReadOnlyApi(store_id)
    out: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    def run(kind, fn):
        try:
            out[kind] = fn()
        except Exception as e:  # noqa: BLE001
            errors[kind] = str(e)[:300]

    run("RETURN", lambda: [parse_return(r) for r in _post_order(api, "/post-order/v2/return/search", "members", mk)])
    run("INQUIRY", lambda: [parse_inquiry(m) for m in _post_order(api, "/post-order/v2/inquiry/search", "members", mk)])
    run("CASE", lambda: [parse_case(m) for m in _post_order(api, "/post-order/v2/casemanagement/search", "members", mk)])
    run("CANCEL", lambda: [parse_cancel(c) for c in _post_order(api, "/post-order/v2/cancellation/search", "cancellations", mk)])

    def disputes():
        tokens = for_store(store_id, "https://api.ebay.com/oauth/api_scope " + SCOPE_PREFIX + "sell.payment.dispute")
        d = _apiz_get(tokens, "/sell/fulfillment/v1/payment_dispute_summary", {"limit": 200}, mk)
        return [parse_dispute(x) for x in d.get("paymentDisputeSummaries") or []]

    def refunds():
        since = (utcnow() - timedelta(days=REFUND_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        d = _apiz_get(api.tokens, "/sell/finances/v1/transaction",
                      {"filter": f"transactionType:{{REFUND}},transactionDate:[{since}..]", "limit": 200}, mk)
        return [parse_refund(x) for x in d.get("transactions") or []]

    def feedback():
        root = api.trading("GetFeedback", "<DetailLevel>ReturnAll</DetailLevel><FeedbackType>FeedbackReceivedAsSeller</FeedbackType>"
                                          f"<Pagination><EntriesPerPage>{FEEDBACK_ENTRIES}</EntriesPerPage><PageNumber>1</PageNumber></Pagination>", mk)
        if root.findtext("e:Ack", namespaces=NS) == "Failure":
            raise RuntimeError(root.findtext("e:Errors/e:ShortMessage", namespaces=NS) or "GetFeedback 失败")
        return [parse_feedback(el, NS) for el in root.findall("e:FeedbackDetailArray/e:FeedbackDetail", NS)]

    run("PAYMENT_DISPUTE", disputes)
    run("REFUND", refunds)
    run("FEEDBACK", feedback)
    return out, errors


FIELDS = ("order_id", "item_id", "buyer", "status", "is_open", "opened_at", "due_at", "amount", "currency", "reason",
          "title", "comment", "rating", "detail", "ebay_updated_at")


def save(db: Session, store_id: str, kind: str, rows: list[dict]) -> dict:
    """按（店铺、类型、编号）更新；返回新增、状态变化数量。"""
    now = utcnow()
    existing = {c.external_id: c for c in db.scalars(select(EbayCase).where(EbayCase.store_id == store_id, EbayCase.kind == kind))}
    new = changed = 0
    for r in rows:
        if not r.get("external_id") or r["external_id"] == "None":
            continue
        c = existing.get(r["external_id"])
        if c is None:
            c = EbayCase(store_id=store_id, kind=kind, external_id=r["external_id"], first_seen_at=now, status_changed_at=now)
            db.add(c)
            existing[r["external_id"]] = c
            new += 1
        elif c.status != r.get("status"):
            c.status_changed_at, c.seen_at, c.seen_by = now, None, None  # 状态变了：重新算“新”
            changed += 1
        for k in FIELDS:
            if k in r:
                setattr(c, k, r[k])
        c.last_seen_at = now
    db.flush()
    return {"new": new, "changed": changed, "total": len(rows)}


def sync_all() -> dict:
    """拉全部有秘钥的 eBay 店铺。"""
    from .security.store_credentials import ebay_store_rows

    with session_scope() as db:
        stores = [(c.store_id, (db.get(Store, c.store_id).default_marketplace_id if db.get(Store, c.store_id) else None))
                  for c in ebay_store_rows(db)]
    stats = {"stores": len(stores), "new": 0, "changed": 0, "errors": {}}
    from .case_extras import refresh_listings
    from .customers import sync_orders

    stats["orders_new"] = stats["listings_fetched"] = stats["listings_pending"] = 0
    from .ebay.stores import ebay_store_targets

    synced = {t.store_id for t in ebay_store_targets()}
    for store_id, mk in stores:
        data, errors = fetch_store(store_id, mk)
        try:  # 客户管理用的订单汇总
            stats["orders_new"] += sync_orders(store_id, mk)["new"]
        except Exception as e:  # noqa: BLE001
            errors["ORDERS"] = str(e)[:300]
        with session_scope() as db:
            for kind, rows in data.items():
                r = save(db, store_id, kind, rows)
                stats["new"] += r["new"]
                stats["changed"] += r["changed"]
            if store_id in synced:  # 异常包裹跟进表：只登记拉消息的店铺
                try:
                    from .parcel_auto import from_cases

                    with db.begin_nested():
                        from_cases(db, store_id)
                except Exception as e:  # noqa: BLE001
                    errors["FOLLOWUPS"] = str(e)[:300]
        try:  # 售后列表的商品图、标题：补拉新出现的商品（存库，打开页面时不再实时查）
            r = refresh_listings(store_id)
            stats["listings_fetched"] += r["fetched"]
            stats["listings_pending"] += r["pending"]
        except Exception as e:  # noqa: BLE001
            errors["LISTINGS"] = str(e)[:300]
        if errors:
            stats["errors"][store_id] = errors
    with session_scope() as db:
        db.add(AuditEvent(actor="ebay_cases", action="EBAY_CASES_SYNC",
                          detail={**stats, "errors": {k: list(v) for k, v in stats["errors"].items()}}))
    return stats


class CasesWorker:
    """每 EBAY_CASES_SYNC_MINUTES 分钟拉一次售后数据；0 = 不自动拉（页面上仍可手动刷新）。"""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last: dict | None = None
        self.last_at: datetime | None = None
        self.running_now = False

    def start(self) -> bool:
        from .ebay.stores import ebay_not_ready_reasons

        if ebay_not_ready_reasons():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ebay-cases", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _due(self) -> bool:
        if get_settings().ebay_cases_sync_minutes <= 0:  # 0 = 暂停自动拉（页面上仍可立即运行）
            return False
        with session_scope() as db:
            last = db.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == "EBAY_CASES_SYNC"))
        return last is None or utcnow() - last >= timedelta(minutes=get_settings().ebay_cases_sync_minutes)

    def run_now(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"busy": True}
        self.running_now = True
        try:
            self.last = sync_all()
        except Exception as e:  # noqa: BLE001
            log.exception("售后数据同步失败")
            self.last = {"error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self.last_at = utcnow()
            self.running_now = False
            self._lock.release()
        return self.last

    def run_in_background(self) -> bool:
        if self.running_now:
            return False
        threading.Thread(target=self.run_now, name="ebay-cases-now", daemon=True).start()
        return True

    def _loop(self) -> None:
        self._stop.wait(30)  # 等启动完成
        while not self._stop.is_set():
            try:
                if self._due():
                    self.run_now()
            except Exception:  # noqa: BLE001
                log.exception("售后数据同步检查失败")
            self._stop.wait(60)


worker = CasesWorker()
