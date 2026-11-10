"""上线前基线的售后补拉（开发方案第 2.1 节“沟通后升级率”）：按创建时间从 eBay 只读补拉对接店铺基线期新开的
咨询（INR）/ 纠纷 / 退货 / 付款纠纷，写进现有 ebay_cases 表。

为什么要补：售后同步（ebay_cases.fetch_store）不带时间条件、每类最多 5 页，覆盖不到上线前 8 周（pieces_saleshop 的退货只到 10-14）。

eBay 接口（2026-11-10 查官方文档）：
- Post-Order return / inquiry / casemanagement search：按创建时间 creation_date_range_from/to、inquiry_creation_date_range_from/to、
  case_creation_date_range_from/to（ISO 8601，最早 18 个月前；只给起点时往后 90 天）；limit 最多 200；
  offset 文档写“0 起的序号，0 或不传是第 1 页、值不超过总页数”，含义不清，这里从 0 开始逐页取、按编号去重，并记下 0 和 1 是否同一页；
- Fulfillment payment_dispute_summary（apiz 主机，sell.payment.dispute 权限）：open_date_from/to，一次最多 90 天、最早 18 个月前；
  limit 最多 200，offset 是跳过的条数；不带条件时只返回“未结和最近关闭的”。

写库规则：只插入 ebay_cases 里还没有的（按 店铺、类型、eBay 编号）；已有的一律不动（同步在维护它们，处理状态、星标、备注、已看都不碰）。
新插入的用 ebay_cases.parse_* 解析、ebay_cases.save 写入，之后标“已看”（seen_by=基线补拉，不在售后管理里冒成一堆“新”），
状态变化时间记为 eBay 的创建时间，detail.origin=BASELINE_BACKFILL；异常包裹跟进（parcel_auto.from_cases）跳过这些旧单。
默认试运行：只统计数量和接口调用次数，不写库。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope, utcnow
from .ebay_cases import parse_case, parse_dispute, parse_inquiry, parse_return, save
from .models import EbayCase

log = logging.getLogger(__name__)

ORIGIN = "BASELINE_BACKFILL"
ORIGIN_90D = "CASE_BACKFILL_90D"  # 2026-11-10 售后原因分类用：全部店铺最近 90 天
BACKFILL_ORIGINS = (ORIGIN, ORIGIN_90D)  # 补拉来的单（异常包裹跟进跳过、基线升级率当作已补拉）
DISPUTE_WINDOW = timedelta(days=89)  # 付款纠纷一次最多查 90 天：按 89 天一段分开查
SEEN_BY = "基线补拉"
DEFAULT_START = datetime(2026, 9, 10)
DEFAULT_END = datetime(2026, 11, 5)  # 不含
PAGE = 200
MAX_PAGES = 50
DISPUTE_SCOPE_EXTRA = "sell.payment.dispute"
# 类型 → (接口, 列表字段, 起点参数, 终点参数, 解析)
POST_ORDER = {
    "RETURN": ("/post-order/v2/return/search", "members", "creation_date_range_from", "creation_date_range_to", parse_return),
    "INQUIRY": ("/post-order/v2/inquiry/search", "members", "inquiry_creation_date_range_from",
                "inquiry_creation_date_range_to", parse_inquiry),
    "CASE": ("/post-order/v2/casemanagement/search", "members", "case_creation_date_range_from",
             "case_creation_date_range_to", parse_case),
}
KINDS = (*POST_ORDER, "PAYMENT_DISPUTE")


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _id_of(kind: str, item: dict) -> str | None:
    key = {"RETURN": "returnId", "INQUIRY": "inquiryId", "CASE": "caseId", "PAYMENT_DISPUTE": "paymentDisputeId"}[kind]
    return str(item.get(key)) if item.get(key) is not None else None


def _default_api(store_id: str):
    from .sources.ebay import EbayReadOnlyApi

    return EbayReadOnlyApi(store_id)


def _default_dispute_tokens(store_id: str):
    from .ebay.auth import SCOPE_PREFIX, for_store

    return for_store(store_id, "https://api.ebay.com/oauth/api_scope " + SCOPE_PREFIX + DISPUTE_SCOPE_EXTRA)


class CaseBackfill:
    def __init__(self, api_factory=None, dispute_tokens=None):
        self.api_factory = api_factory or _default_api
        self.dispute_tokens = dispute_tokens or _default_dispute_tokens

    # ------------------------------------------------------------ 拉取（只读）
    def _post_order(self, api, kind: str, start: datetime, end: datetime, mk: str | None, info: dict) -> list[dict]:
        """逐页取。offset 的含义文档写得不清：先取 offset=0、1 两页判断——
        两页相同 = 页码从 1 起（0 和 1 都是第 1 页）；第 2 页是第 1 页错开一条 = 跳过的条数；否则 = 页码从 0 起。"""
        path, key, p_from, p_to, _ = POST_ORDER[kind]
        rows: dict[str, dict] = {}

        def page(offset: int) -> list[dict]:
            d = api.get(path, {p_from: _ts(start), p_to: _ts(end), "limit": PAGE, "offset": offset},
                        auth="IAF", marketplace=mk) or {}
            items = d.get(key) or []
            pag = d.get("paginationOutput") or {}
            if pag.get("totalEntries") is not None:
                info["total_reported"] = pag.get("totalEntries")
            for x in items:
                if _id_of(kind, x):
                    rows.setdefault(_id_of(kind, x), x)
            return items

        first = page(0)
        if len(first) < PAGE:
            return list(rows.values())
        second = page(1)
        ids0, ids1 = [_id_of(kind, x) for x in first], [_id_of(kind, x) for x in second]
        if ids1 == ids0:
            mode, offsets = "page_from_1", range(2, MAX_PAGES + 1)
        elif ids1[:-1] == ids0[1:]:
            mode, offsets = "skip_records", range(PAGE, PAGE * MAX_PAGES, PAGE)
        else:
            mode, offsets = "page_from_0", range(2, MAX_PAGES)
            if len(second) < PAGE:
                offsets = range(0)
        info["offset_mode"] = mode
        for off in offsets:
            before = len(rows)
            items = page(off)
            if len(items) < PAGE or len(rows) == before:
                break
        return list(rows.values())

    def _disputes(self, api, store_id: str, start: datetime, end: datetime, mk: str | None) -> list[dict]:
        from .sources import SourceNotAvailable

        tokens = self.dispute_tokens(store_id)
        base = get_settings().ebay_api_base.replace("://api.", "://apiz.")
        rows: dict[str, dict] = {}
        lo = start
        while lo < end:
            hi = min(end, lo + DISPUTE_WINDOW)
            for page in range(MAX_PAGES):
                api.calls += 1
                r = api.http.get(f"{base}/sell/fulfillment/v1/payment_dispute_summary",
                                 params={"open_date_from": _ts(lo), "open_date_to": _ts(hi), "limit": PAGE,
                                         "offset": page * PAGE},
                                 headers={"Authorization": f"Bearer {tokens.access_token()}", "Accept": "application/json",
                                          **({"X-EBAY-C-MARKETPLACE-ID": mk} if mk else {})})
                if r.status_code == 204:
                    break
                if r.status_code != 200:
                    raise SourceNotAvailable(f"eBay 接口 payment_dispute_summary 返回 HTTP {r.status_code}")
                items = r.json().get("paymentDisputeSummaries") or []
                for x in items:
                    if _id_of("PAYMENT_DISPUTE", x):
                        rows[_id_of("PAYMENT_DISPUTE", x)] = x
                if len(items) < PAGE:
                    break
            lo = hi
        return list(rows.values())

    def case_type(self, api, case_id: str, mk: str | None) -> str | None:
        """纠纷详情（GET casemanagement/{caseId}）里的 caseType：ITEM_NOT_RECEIVED 未收到货 / RETURN 退货升级。"""
        d = api.get(f"/post-order/v2/casemanagement/{case_id}", None, auth="IAF", marketplace=mk) or {}
        return d.get("caseType")

    def fetch(self, store_id: str, mk: str | None, start: datetime, end: datetime) -> tuple[dict, dict, int]:
        """一个店铺 4 类售后在时间段内新开的单。返回 ({类型: [解析后的记录]}, {类型: 信息 / 错误}, 接口调用数)。"""
        api = self.api_factory(store_id)
        out: dict[str, list[dict]] = {}
        info: dict[str, dict] = {}
        for kind in POST_ORDER:
            info[kind] = {}
            try:
                raw = self._post_order(api, kind, start, end, mk, info[kind])
                out[kind] = [POST_ORDER[kind][4](x) for x in raw]
            except Exception as e:  # noqa: BLE001  某一类失败不影响其他类
                info[kind]["error"] = str(e)[:300]
        info["PAYMENT_DISPUTE"] = {}
        try:
            out["PAYMENT_DISPUTE"] = [parse_dispute(x) for x in self._disputes(api, store_id, start, end, mk)]
        except Exception as e:  # noqa: BLE001
            info["PAYMENT_DISPUTE"]["error"] = str(e)[:300]
        for kind, rows in out.items():  # 接口按创建时间过滤；再按解析出的开始时间核一遍（缺时间的保留）
            out[kind] = [r for r in rows if r.get("opened_at") is None or start <= r["opened_at"] < end]
        return out, info, api.calls

    # ------------------------------------------------------------ 入口
    def run(self, store_keys: set[str] | None = None, start: datetime = DEFAULT_START, end: datetime = DEFAULT_END,
            apply: bool = False, origin: str = ORIGIN, case_details: bool = False) -> dict:
        """store_keys={"*"} = 全部有秘钥的店铺。case_details：时间段内还不知道类型的纠纷逐张查详情补 caseType
        （写进 reason；新拉到的和库里已有的都补，别的字段不动；试运行只统计要查几张，不调用）。"""
        from .ebay.stores import ebay_store_targets

        keys = store_keys or get_settings().ebay_live_store_keys
        if not keys:
            return {"ok": False, "error": "没有指定店铺，EBAY_LIVE_STORES 也是空的"}
        targets = ebay_store_targets(keys)
        if not targets:
            return {"ok": False, "error": f"找不到可对接的店铺：{'、'.join(sorted(keys))}"}
        report: dict = {"ok": True, "apply": apply, "period_start": _ts(start), "period_end": _ts(end), "origin": origin,
                        "stores": {}, "api_calls": 0, "case_detail_calls": 0, "case_details_needed": 0}
        for t in targets:
            st: dict = {"name": t.name, "before": counts(t.store_id, start, end)}
            data, info, calls = self.fetch(t.store_id, t.marketplace_id, start, end)
            report["api_calls"] += calls
            st["api_calls"], st["info"] = calls, info
            with session_scope() as db:
                kinds = {}
                for kind, rows in data.items():
                    have = set(db.scalars(select(EbayCase.external_id).where(
                        EbayCase.store_id == t.store_id, EbayCase.kind == kind)))
                    new_rows = [r for r in rows if r.get("external_id") and r["external_id"] != "None"
                                and r["external_id"] not in have]
                    if kind == "CASE" and case_details and new_rows:  # 新拉到的纠纷先查类型
                        report["case_details_needed"] += len(new_rows)
                        if apply:
                            self._case_types(t, new_rows, st, report)
                    kinds[kind] = {"found": len(rows), "already": len(rows) - len(new_rows), "new": len(new_rows),
                                   "new_open": sum(bool(r.get("is_open")) for r in new_rows)}
                    if apply and new_rows:
                        save(db, t.store_id, kind, new_rows)
                        now = utcnow()
                        for c in db.scalars(select(EbayCase).where(
                                EbayCase.store_id == t.store_id, EbayCase.kind == kind,
                                EbayCase.external_id.in_([r["external_id"] for r in new_rows]))):
                            c.seen_at, c.seen_by = now, SEEN_BY
                            c.status_changed_at = c.opened_at or now
                            c.detail = {**(c.detail or {}), "origin": origin}
                if case_details:  # 库里已有、还不知道类型的纠纷：只补 reason
                    old = list(db.scalars(select(EbayCase).where(
                        EbayCase.store_id == t.store_id, EbayCase.kind == "CASE", EbayCase.reason.is_(None),
                        EbayCase.opened_at >= start, EbayCase.opened_at < end)))  # 刚插入的新单已带类型，不会重复查
                    report["case_details_needed"] += len(old)
                    st["old_cases_without_type"] = len(old)
                    if apply and old:
                        rows_old = [{"external_id": c.external_id} for c in old]
                        self._case_types(t, rows_old, st, report)
                        got = {r["external_id"]: r.get("reason") for r in rows_old}
                        for c in old:
                            c.reason = got.get(c.external_id) or c.reason
                if not apply:
                    db.rollback()
            st["kinds"] = kinds
            if any(v.get("error") for v in info.values()):
                report["ok"] = False
            st["after"] = counts(t.store_id, start, end) if apply else None
            report["stores"][t.store_id] = st
        report["api_calls"] += report["case_detail_calls"]
        report["estimated_apply_calls"] = report["api_calls"] - report["case_detail_calls"] + report["case_details_needed"]
        return report

    def _case_types(self, t, rows: list[dict], st: dict, report: dict) -> None:
        api = self.api_factory(t.store_id)
        types = st.setdefault("case_types", {})
        for r in rows:
            try:
                r["reason"] = self.case_type(api, r["external_id"], t.marketplace_id) or r.get("reason")
            except Exception as e:  # noqa: BLE001  某张查不到不影响其他
                st.setdefault("case_type_errors", []).append(str(e)[:120])
            types[r.get("reason") or "?"] = types.get(r.get("reason") or "?", 0) + 1
        report["case_detail_calls"] += api.calls
        st["api_calls"] = st.get("api_calls", 0) + api.calls


def counts(store_id: str, start: datetime, end: datetime) -> dict:
    """ebay_cases 里这个店铺时间段内新开的各类售后数量。"""
    with session_scope() as db:
        rows = db.execute(select(EbayCase.kind, func.count()).where(
            EbayCase.store_id == store_id, EbayCase.kind.in_(KINDS), EbayCase.opened_at >= start,
            EbayCase.opened_at < end).group_by(EbayCase.kind)).all()
    return {k: 0 for k in KINDS} | {k: n for k, n in rows}
