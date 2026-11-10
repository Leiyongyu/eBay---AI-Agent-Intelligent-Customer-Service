"""基线售后补拉（case_backfill.py）：假 eBay 接口，不联网。
覆盖：按创建时间查询、三种 offset 含义都能翻全页、去重、试运行不写库、只插入没有的（已有单的人工字段不动）、
新插入的标已看且带来源、异常包裹跟进跳过补拉的旧单、付款纠纷按条数翻页。"""
from datetime import datetime

import httpx
import pytest

from app import config

START, END = datetime(2026, 9, 10), datetime(2026, 11, 5)


def _ret(i, day, reason="NO_LONGER_NEED_ITEM", state="CLOSED"):
    return {"returnId": f"R{i}", "orderId": f"01-{i}", "buyerLoginName": f"b{i}", "state": state, "status": state,
            "creationInfo": {"item": {"itemId": "110"}, "reason": reason,
                             "creationDate": {"value": f"2026-{day}T10:00:00.000Z"}}}


RETURNS = [_ret(i, d) for i, d in enumerate(["09-11", "09-20", "10-01", "10-10", "10-20"], 1)] + \
          [_ret(6, "11-02", "ARRIVED_DAMAGED", "RETURN_STARTED")]
INQS = [{"inquiryId": 71, "itemId": 110, "transactionId": 1, "buyer": "bi", "inquiryStatusEnum": "CLOSED",
         "creationDate": {"value": "2026-09-15T00:00:00.000Z"}}]
DISPUTES = [{"paymentDisputeId": f"PD{i}", "paymentDisputeStatus": "CLOSED", "openDate": "2026-10-0%dT00:00:00.000Z" % i}
            for i in range(1, 4)]


class FakeEbay:
    def __init__(self, mode="page_from_1"):
        self.mode = mode
        self.requests: list[httpx.Request] = []

    def _page(self, items, offset, limit):
        if self.mode == "page_from_1":
            start = max(offset - 1, 0) * limit
        elif self.mode == "page_from_0":
            start = offset * limit
        else:  # skip_records
            start = offset
        return items[start: start + limit]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET", "只读：只能发 GET"
        q = dict(request.url.params)
        path = request.url.path
        lim, off = int(q["limit"]), int(q["offset"])
        if path.endswith("/payment_dispute_summary"):
            assert q["open_date_from"] == "2026-09-10T00:00:00.000Z" and q["open_date_to"] == "2026-11-05T00:00:00.000Z"
            return httpx.Response(200, json={"paymentDisputeSummaries": DISPUTES[off: off + lim]})
        if path.endswith("/return/search"):
            assert q["creation_date_range_from"] == "2026-09-10T00:00:00.000Z"
            assert q["creation_date_range_to"] == "2026-11-05T00:00:00.000Z"
            return httpx.Response(200, json={"members": self._page(RETURNS, off, lim),
                                             "paginationOutput": {"totalEntries": len(RETURNS)}})
        if path.endswith("/inquiry/search"):
            assert "inquiry_creation_date_range_from" in q
            return httpx.Response(200, json={"members": self._page(INQS, off, lim)})
        if path.endswith("/casemanagement/search"):
            assert "case_creation_date_range_to" in q
            return httpx.Response(200, json={"members": []})
        return httpx.Response(404)


class Tokens:
    def access_token(self):
        return "AT"

    def invalidate(self):
        pass


@pytest.fixture()
def cb(app_env, monkeypatch):
    from app import case_backfill

    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-id")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "v^1.1#fake")
    monkeypatch.setenv("EBAY_STORE_ID", "REAL_US")
    config.get_settings.cache_clear()
    monkeypatch.setattr(case_backfill, "PAGE", 2)  # 每页 2 条，测翻页
    yield
    config.get_settings.cache_clear()


def make(fake):
    from app.case_backfill import CaseBackfill
    from app.sources.ebay import EbayReadOnlyApi

    http = httpx.Client(transport=httpx.MockTransport(fake))
    return CaseBackfill(api_factory=lambda sid: EbayReadOnlyApi(sid, http=http, tokens=Tokens()),
                        dispute_tokens=lambda sid: Tokens())


def run(fake, **kw):
    return make(fake).run(store_keys={"real_us"}, start=START, end=END, **kw)


def _cases():
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EbayCase

    with session_scope() as db:
        return {(c.kind, c.external_id): {k: getattr(c, k) for k in ("status", "seen_by", "starred", "note", "work_state",
                                                                    "status_changed_at", "opened_at", "detail")}
                for c in db.scalars(select(EbayCase))}


@pytest.mark.parametrize("mode", ["page_from_1", "page_from_0", "skip_records"])
def test_dry_run_reads_all_pages_without_writing(cb, mode):
    fake = FakeEbay(mode)
    r = run(fake)
    st = r["stores"]["REAL_US"]
    assert r["ok"] and st["info"]["RETURN"]["offset_mode"] == mode
    assert st["kinds"]["RETURN"] == {"found": 6, "already": 0, "new": 6, "new_open": 1}
    assert st["kinds"]["INQUIRY"]["found"] == 1 and st["kinds"]["CASE"]["found"] == 0
    assert st["kinds"]["PAYMENT_DISPUTE"]["found"] == 3
    assert r["api_calls"] == len(fake.requests)
    assert st["before"]["RETURN"] == 0 and st["after"] is None
    assert _cases() == {}


def test_apply_inserts_only_missing_and_keeps_manual_fields(cb):
    from app import ebay_cases as ec
    from app.case_backfill import ORIGIN, SEEN_BY
    from app.db import session_scope
    from app.models import EbayCase
    from sqlalchemy import select

    with session_scope() as db:  # 同步已经拉到过的单，客服处理过
        ec.save(db, "REAL_US", "RETURN", [ec.parse_return({**RETURNS[4], "state": "RETURN_STARTED", "status": "OPEN"})])
        c = db.scalar(select(EbayCase).where(EbayCase.external_id == "R5"))
        c.starred, c.note, c.work_state, c.seen_by = True, "已联系买家", "DONE", "小王"
    before = _cases()[("RETURN", "R5")]

    r = run(FakeEbay(), apply=True)
    st = r["stores"]["REAL_US"]
    assert st["kinds"]["RETURN"] == {"found": 6, "already": 1, "new": 5, "new_open": 1}
    assert st["before"]["RETURN"] == 1 and st["after"]["RETURN"] == 6
    assert st["after"]["INQUIRY"] == 1 and st["after"]["PAYMENT_DISPUTE"] == 3
    rows = _cases()
    assert rows[("RETURN", "R5")] == before  # 已有的单一点没动（状态、星标、备注、处理状态、已看）
    new = rows[("RETURN", "R1")]
    assert new["seen_by"] == SEEN_BY and new["detail"]["origin"] == ORIGIN
    assert new["status_changed_at"] == new["opened_at"] == datetime(2026, 9, 11, 10)
    again = run(FakeEbay(), apply=True)["stores"]["REAL_US"]  # 重跑幂等
    assert again["kinds"]["RETURN"]["new"] == 0 and again["after"] == st["after"]


def test_parcel_followups_skip_backfilled(cb):
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.parcel_auto import from_cases
    from conftest import make_user

    make_user("boss", "ADMIN")  # 跟进的负责人兜底是管理员
    now = datetime(2026, 11, 10)
    run(FakeEbay(), apply=True)  # R6：11-02 新开、到货损坏、未结——补拉来的，不登记
    with session_scope() as db:
        assert from_cases(db, "REAL_US", now=now)["created_or_updated"] == 0
        # 同样的单如果是同步拉到的（没有补拉来源）就会登记
        ec.save(db, "REAL_US", "RETURN", [ec.parse_return({**RETURNS[5], "returnId": "R99", "orderId": "01-99"})])
        assert from_cases(db, "REAL_US", now=now)["created_or_updated"] == 1
