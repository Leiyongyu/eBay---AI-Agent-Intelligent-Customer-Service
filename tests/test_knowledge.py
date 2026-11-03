"""全量注入模式的过滤规则，用 tests/fixtures/knowledge_sample.jsonl 的小样例验证。"""
from datetime import date
from pathlib import Path

import pytest

from app.db import session_scope
from app.knowledge.loader import import_jsonl
from app.knowledge.retriever import load_full_context

SAMPLE = Path(__file__).parent / "fixtures" / "knowledge_sample.jsonl"
DAY = date(2026, 10, 26)


@pytest.fixture()
def sample(app_env):
    with session_scope() as db:
        import_jsonl(db, SAMPLE)


def ids(store, site, on=DAY):
    with session_scope() as db:
        return {c.clause_id for c in load_full_context(db, store, site, on, 10**9).clauses}


def test_store_site_audience_state_and_dates(sample):
    assert ids("S1", "EBAY_DE") == {"T-ALL-1", "T-S1-DE"}  # 其他店铺、其他站点、内部 SOP、草稿、过期、未生效都排除
    assert ids("S1", "EBAY_GB") == {"T-ALL-1", "T-S1-GB"}
    assert ids("S2", "EBAY_DE") == {"T-ALL-1", "T-S2-DE"}


def test_unknown_marketplace_only_all_sites(sample):
    assert ids("S1", "UNKNOWN") == {"T-ALL-1"}


def test_future_clause_after_effective_date(sample):
    assert "T-S1-FUTURE" in ids("S1", "EBAY_DE", date(2026, 12, 2))


def test_budget_flag(sample):
    with session_scope() as db:
        assert load_full_context(db, "S1", "EBAY_DE", DAY, 1).over_budget
