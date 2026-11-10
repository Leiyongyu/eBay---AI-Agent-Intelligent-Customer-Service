"""验收（测试工程师，2026-11-10）：上线前基线补拉 + 第 2 章指标 / 基线对比 / 快照——补开发用例没覆盖到的点。
致谢误判、时段边界、同一张售后算几轮、观察中走完后重算、新增批次后冻结快照、基线清理不碰工作台、
补拉售后单的来源标记被常规同步冲掉、权限。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from conftest import login, make_user
from test_launch_quality import _create_baseline, bj, conv

H = 3600


# ---------------------------------------------------------------- 致谢判断（基线没有 AI 意图，只靠关键词）
@pytest.mark.parametrize("text", ["Lo siento quiero la devolución del dinero, gracias.",
                                  "I received the wrong part, the flange is different.",
                                  "Ok, I want a refund please."], ids=["es_refund", "en_wrong_part", "en_refund"])
def test_complaint_with_thanks_word_not_thanks_only(text):
    from app.round_metrics import is_thanks_only

    assert not is_thanks_only(text)


def test_thanks_word_lists():
    """记录现状：带 noch / no / 没 的致谢客套话不算“只是致谢”（会被算成追问，偏严，影响小）。"""
    from app.round_metrics import is_thanks_only

    for t in ("Danke, noch einen schönen Tag!", "Thank you, no problem!", "谢谢，没问题"):
        assert not is_thanks_only(t), t
    for t in ("Nein danke", "Ok Danke", "Gracias…", "Many thanks, I got it !"):
        assert is_thanks_only(t), t


# ---------------------------------------------------------------- 时段边界
@pytest.mark.parametrize("at, want", [
    (bj(2026, 11, 13, 17, 59), "work"),    # 周五 17:59
    (bj(2026, 11, 13, 18, 0), "night"),    # 周五 18:00 → 工作日非工作时段
    (bj(2026, 11, 14, 0, 0), "weekend"),   # 周六 00:00
    (bj(2026, 11, 15, 23, 59), "weekend"),  # 周日 23:59
    (bj(2026, 11, 16, 0, 0), "night"),     # 周一 00:00
    (bj(2026, 11, 16, 8, 59), "night"),
    (bj(2026, 11, 16, 9, 0), "work"),
])
def test_band_more_boundaries(at, want):
    from app.round_metrics import band

    assert band(at) == want
    assert band(at.replace(tzinfo=timezone.utc)) == want  # 带时区和不带时区一致


def test_weekend_counts_in_night_backlog():
    """“非工作时段积压”（2026-11-10 由“夜间积压”改名，字段名仍是 night_backlog）包含周末到达、周一工作时段才回的轮次。"""
    from app.round_metrics import build_rounds, compute

    sat = bj(2026, 11, 14, 11)
    r = compute(build_rounds([conv("W", [(sat, "BUYER"), (bj(2026, 11, 16, 10), "SELLER")])]), {}, bj(2026, 12, 1, 10), [], 7)
    assert r["night_backlog"]["count"] == 1 and r["bands"][2]["key"] == "weekend" and r["bands"][2]["rounds"] == 1


def test_one_case_counts_for_every_round_in_window():
    """一张售后只算一轮（2026-11-10 定）：同一张退货落在两轮回复后的 7 天内，只归到开启前最近的那一轮
    （线上基线 tm58_43 原来 1 张退货算 2 轮）。"""
    from app.round_metrics import build_rounds, compute

    t0 = bj(2026, 9, 11, 10)
    rounds = build_rounds([conv("T", [(t0, "BUYER"), (t0 + timedelta(hours=15), "SELLER"),
                                      (t0 + timedelta(days=3), "BUYER", "It does not fit"),
                                      (t0 + timedelta(days=3, hours=1), "SELLER")], buyer="tm")])
    r = compute(rounds, {("STORE_A", "tm"): [t0 + timedelta(days=4)]}, bj(2026, 12, 1, 10), [], 7)
    assert r["escalation"]["escalated"] == 1 and r["escalation"]["not_escalated"] == 1


def test_follow_up_counts_requested_info():
    """追问排除补资料（2026-11-10 定）：我们请买家发 VIN，买家 48 小时内发来 VIN 不算“被追问”
    （线上基线抽查 10 轮里约 6 轮是这类）。"""
    from app.round_metrics import build_rounds, compute

    t0 = bj(2026, 10, 13, 10)
    rounds = build_rounds([conv("V", [(t0, "BUYER", "Does it fit my car?"), (t0 + timedelta(hours=1), "SELLER", "Please send VIN"),
                                      (t0 + timedelta(hours=5), "BUYER", "WVWZZZ1KZBW000123"), (t0 + timedelta(hours=6), "SELLER")])])
    fu = compute(rounds, {}, bj(2026, 12, 1, 10), [], 7)["follow_up"]
    assert fu["followed"] == 0 and fu["not_followed"] == 2


# ---------------------------------------------------------------- 快照：观察中走完后重算；新增批次
class _Clock:
    def __init__(self, monkeypatch, at):
        from app import launch_metrics

        real = launch_metrics.datetime
        clock = self

        class Fake(real):
            @classmethod
            def now(cls, tz=None):
                return clock.at.replace(tzinfo=timezone.utc) if tz else clock.at

        self.at = at
        monkeypatch.setattr(launch_metrics, "datetime", Fake)


def _late_baseline():
    """基线最后一天（11-04）的一轮：回复后 7 天升级窗口要到 11-11 才走完。"""
    from app.db import session_scope
    from app.models import BaselineBatch, BaselineConversation, BaselineMessage

    with session_scope() as db:
        db.add(BaselineBatch(stores=["STORE_A"], period_start=datetime(2026, 9, 10), period_end=datetime(2026, 11, 5), status="DONE"))
        c = BaselineConversation(store_id="STORE_A", ebay_conversation_id="LATE", buyer_key="lt", conversation_type="FROM_MEMBERS")
        db.add(c)
        db.flush()
        for i, (role, at) in enumerate((("BUYER", bj(2026, 11, 4, 10)), ("SELLER", bj(2026, 11, 4, 12))), 1):
            db.add(BaselineMessage(store_id="STORE_A", conversation_id=c.id, ebay_message_id=f"LT{i}", sender_role=role,
                                   sent_at=at, body="x"))


def test_snapshot_recomputed_after_observing_window(client, monkeypatch):
    from app import launch_metrics
    from app.db import session_scope
    from app.models import BaselineSnapshot

    _late_baseline()
    clock = _Clock(monkeypatch, datetime(2026, 11, 6, 12))
    with session_scope() as db:
        launch_metrics.refresh_baseline_snapshots(db)
    with session_scope() as db:
        s = db.scalar(select(BaselineSnapshot).where(BaselineSnapshot.store_id == "*"))
        assert s.observing > 0 and s.metrics["escalation"]["observing"] == 1
        first = s.computed_at
    clock.at = datetime(2026, 11, 20, 12)  # 7 天窗口走完
    with session_scope() as db:
        r = launch_metrics.refresh_baseline_snapshots(db)
    assert {x["action"] for x in r} == {"computed"}
    with session_scope() as db:
        s = db.scalar(select(BaselineSnapshot).where(BaselineSnapshot.store_id == "*"))
        assert s.observing == 0 and s.metrics["escalation"]["not_escalated"] == 1 and s.computed_at > first
    with session_scope() as db:  # 走完之后不再重算
        assert {x["action"] for x in launch_metrics.refresh_baseline_snapshots(db)} == {"kept"}


def test_frozen_snapshot_survives_new_batch(client):
    from sqlalchemy import delete

    from app.db import session_scope
    from app.models import BaselineBatch, BaselineMessage

    _create_baseline()
    assert client.get("/api/performance/launch").json()["baseline"]["data"]["rounds"] == 3
    with session_scope() as db:
        db.execute(delete(BaselineMessage))  # 90 天清理
        db.add(BaselineBatch(stores=["STORE_C"], period_start=datetime(2026, 9, 10), period_end=datetime(2026, 11, 5),
                             status="DONE"))
    b = client.get("/api/performance/launch").json()["baseline"]["data"]
    assert b is not None and b["rounds"] == 3


def test_snapshot_has_no_message_text(client):
    from app.db import session_scope
    from app.models import BaselineSnapshot

    _create_baseline()
    client.get("/api/performance/launch")
    with session_scope() as db:
        blob = " ".join(str(s.metrics) for s in db.scalars(select(BaselineSnapshot)))
    for body in ("Where is it", "On the way", "Still not here", "Checking", "Hello", "bb", "bc"):
        assert body not in blob, body


# ---------------------------------------------------------------- 基线清理不碰工作台数据
def test_baseline_cleanup_only_baseline_tables(client, monkeypatch):
    from app import baseline
    from app.db import session_scope, utcnow
    from app.models import BaselineBatch, BaselineConversation, BaselineMessage, Conversation, Message, ReplyJob

    _create_baseline()
    with session_scope() as db:
        c = Conversation(store_id="STORE_A", ebay_conversation_id="WB-OLD", buyer_key="old", conversation_type="FROM_MEMBERS")
        db.add(c)
        db.flush()
        db.add(Message(store_id="STORE_A", ebay_message_id="WB-OLD-1", conversation_id=c.id, conversation_seq=1,
                       sender_role="BUYER", direction="IN", sent_at=datetime(2026, 9, 1), body="old workbench"))
        db.add(ReplyJob(store_id="STORE_A", conversation_id=c.id, anchor_source_message_id="WB-OLD-1", state="REVIEW_REQUIRED"))
        before = (db.scalar(select(func.count()).select_from(Message)), db.scalar(select(func.count()).select_from(Conversation)),
                  db.scalar(select(func.count()).select_from(ReplyJob)))
        n_base = db.scalar(select(func.count()).select_from(BaselineMessage))
    now = datetime(2027, 2, 1)  # 截止 11-03：基线消息（09-01 ~ 10-13）都超过 90 天
    dry = baseline.cleanup(now, dry_run=True)
    assert dry["messages"] == n_base
    with session_scope() as db:
        assert db.scalar(select(func.count()).select_from(BaselineMessage)) == n_base  # 预览不删
    out = baseline.cleanup(now, dry_run=False)
    assert out["messages"] == n_base and out["conversations"] == 3
    with session_scope() as db:
        after = (db.scalar(select(func.count()).select_from(Message)), db.scalar(select(func.count()).select_from(Conversation)),
                 db.scalar(select(func.count()).select_from(ReplyJob)))
        assert after == before  # 工作台的会话、消息、任务一条没动
        assert db.scalar(select(func.count()).select_from(BaselineBatch)) == 1  # 批次（只有数量）保留
        assert db.scalar(select(func.count()).select_from(BaselineConversation)) == 0
    monkeypatch.setenv("BASELINE_RETENTION_DAYS", "0")
    from app import config

    config.get_settings.cache_clear()
    assert "skipped" in baseline.cleanup(now, dry_run=False)


# ---------------------------------------------------------------- 补拉的售后单被常规同步更新后
def test_backfilled_case_keeps_origin_after_regular_sync(client):
    from app import ebay_cases, parcel_auto
    from app.db import session_scope
    from app.models import EbayCase, ParcelFollowup

    with session_scope() as db:
        db.add(EbayCase(store_id="STORE_A", kind="INQUIRY", external_id="5300000077", buyer="bk", order_id="26-10001-US",
                        status="OPEN", is_open=True, opened_at=datetime(2026, 10, 20), status_changed_at=datetime(2026, 10, 20),
                        seen_by="基线补拉", detail={"origin": "BASELINE_BACKFILL"}))
    with session_scope() as db:  # 常规同步拉到同一张单，状态变了
        ebay_cases.save(db, "STORE_A", "INQUIRY", [{"external_id": "5300000077", "status": "WAITING_SELLER", "is_open": True,
                                                    "buyer": "bk", "order_id": "26-10001-US",
                                                    "detail": {"type": "INR", "seller_action": "RESPOND"}}])
    with session_scope() as db:
        c = db.scalar(select(EbayCase).where(EbayCase.external_id == "5300000077"))
        assert (c.detail or {}).get("origin") == "BASELINE_BACKFILL"
        parcel_auto.from_cases(db, "STORE_A", now=datetime(2026, 11, 12))
        assert db.scalar(select(func.count()).select_from(ParcelFollowup)) == 0


# ---------------------------------------------------------------- 权限
def test_permissions_baseline_and_escalation_cases(client):
    from app.db import session_scope
    from app.models import EbayCase

    _create_baseline()
    t = bj(2026, 10, 13, 10)
    with session_scope() as db:  # 店铺 A 有一张升级售后（买家 bb，回复后 2 天）；店铺 B 的客服不能算进去也不能看到
        db.add(EbayCase(store_id="STORE_A", kind="RETURN", external_id="RA1", buyer="bb", reason="DEFECTIVE_ITEM",
                        opened_at=t + timedelta(days=2), detail={"origin": "BASELINE_BACKFILL"}))
        db.add(EbayCase(store_id="STORE_A", kind="RETURN", external_id="RA2", buyer="bc", reason="DEFECTIVE_ITEM",
                        opened_at=t + timedelta(days=2)))  # 买家名和店铺 B 的基线买家相同，但店铺不同
    admin = client.get("/api/performance/launch").json()["baseline"]["data"]
    assert admin["escalation"]["escalated"] >= 1
    make_user("cs_only_b", "CS", stores=["STORE_B"])
    cs = TestClient(client.app)
    login(cs, "cs_only_b")
    b = cs.get("/api/performance/launch").json()
    data = b["baseline"]["data"]
    assert data["rounds"] == 1 and data["escalation"]["escalated"] == 0  # 店铺 A 的售后不算进店铺 B
    assert {x["key"] for x in data["volume"]["by_store"]} == {"STORE_B"}
    make_user("cs_none", "CS", stores=[])
    cn = TestClient(client.app)
    login(cn, "cs_none")
    r = cn.get("/api/performance/launch")
    assert r.status_code in (200, 403)
    if r.status_code == 200:
        d = r.json()
        assert not d["baseline"]["available"] or d["baseline"]["data"]["rounds"] == 0
        assert d["quality"]["rounds"] == 0
