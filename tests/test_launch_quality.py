"""上线效果第 2 章指标与基线对比：工作时段 / 夜间 / 周末边界（含跨零点）、致谢排除、追问 48 小时与观察中、
升级 7 天窗口与退货原因、北极星、夜间积压、消息量；基线表缺失 / 有数据时的对比表与店铺过滤；上线后数据用 AI 意图判致谢。"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import login, make_user

H = 3600


def bj(y, mo, d, h, mi=0):
    """北京时间 → UTC（无时区）。"""
    return datetime(y, mo, d, h, mi) - timedelta(hours=8)


def conv(key, msgs, store="STORE_A", buyer="b1"):
    from app.round_metrics import Conv, Msg

    return Conv(key, store, buyer, [Msg(at, role, ack, txt) for at, role, *rest in msgs
                                     for ack, txt in [(rest[1] if len(rest) > 1 else False, rest[0] if rest else "help")]])


def test_band_boundaries_and_midnight():
    from app.round_metrics import band

    assert band(bj(2026, 11, 9, 9, 0)) == "work"  # 周一 9:00
    assert band(bj(2026, 11, 9, 8, 59)) == "night"
    assert band(bj(2026, 11, 9, 17, 59)) == "work" and band(bj(2026, 11, 9, 18, 0)) == "night"
    assert band(bj(2026, 11, 14, 11, 0)) == "weekend"  # 周六
    assert band(datetime(2026, 11, 8, 23, 30)) == "night"  # UTC 周日 23:30 = 北京周一 07:30
    assert band(datetime(2026, 11, 9, 1, 30)) == "work"  # UTC 周一 01:30 = 北京 09:30


def test_thanks_only_detection():
    from app.round_metrics import is_thanks_only

    for t in ("Thanks!", "Vielen Dank, alles klar", "Merci, bien reçu", "Grazie mille", "谢谢，收到了", "ok thank you"):
        assert is_thanks_only(t), t
    for t in ("Thanks, but it still has not arrived", "Danke, aber das Paket ist noch nicht da",
              "Thanks? Where is my parcel?", "谢谢，但是还没收到", "Hello, does it fit my car"):
        assert not is_thanks_only(t), t
    assert is_thanks_only("whatever", "thanks_closing") and not is_thanks_only("Thanks!", "shipping_status")  # 有意图看意图


def test_follow_up_escalation_north_star_and_observing():
    from app.round_metrics import build_rounds, compute

    now = bj(2026, 11, 30, 12)
    t0 = bj(2026, 11, 10, 10)  # 周二工作时段
    convs = [
        # A：1 小时内回复，48 小时内买家又来问（不是致谢）→ 被追问，北极星不达标
        conv("A", [(t0, "BUYER"), (t0 + timedelta(hours=1), "SELLER"), (t0 + timedelta(hours=20), "BUYER", "Still nothing?"),
                   (t0 + timedelta(hours=21), "SELLER")]),
        # B：回复后只回了句“谢谢”→ 不算追问；7 天内没升级 → 北极星达标
        conv("B", [(t0, "BUYER"), (t0 + timedelta(hours=2), "SELLER"), (t0 + timedelta(hours=5), "BUYER", "Thanks a lot!")], buyer="b2"),
        # C：回复后第 3 天开了退货（有缺陷）→ 升级
        conv("C", [(t0, "BUYER"), (t0 + timedelta(hours=3), "SELLER")], buyer="b3"),
        # D：回复后 7 天多才开纠纷 → 不算升级；“已收到”回执不算回复，真正回复在 30 小时后 → 超时
        conv("D", [(t0, "BUYER"), (t0 + timedelta(minutes=5), "SELLER", "ack", True), (t0 + timedelta(hours=30), "SELLER")], buyer="b4"),
        # E：没回、已超过 24 小时 → 超时（北极星不达标）
        conv("E", [(t0, "BUYER")], buyer="b5"),
        # F：刚回复（距 now 10 小时）→ 追问、升级、北极星都观察中
        conv("F", [(now - timedelta(hours=12), "BUYER"), (now - timedelta(hours=10), "SELLER")], buyer="b6"),
        # G：刚来信还没回、没到 24 小时 → 按时观察中
        conv("G", [(now - timedelta(hours=2), "BUYER")], buyer="b7"),
    ]
    rounds = build_rounds(convs)
    assert len(rounds) == 9  # A 两轮、B 两轮（第二轮是致谢）
    # b4：D 在 t0+30 小时回复，纠纷开在回复后第 7 天 18 小时 → 窗口外
    cases = {("STORE_A", "b3"): [t0 + timedelta(days=3)], ("STORE_A", "b4"): [t0 + timedelta(days=9)]}
    r = compute([x for x in rounds if not (x.conv == "B" and x.start > t0)], cases, now, [], 7)
    fu, esc, ns, ot = r["follow_up"], r["escalation"], r["north_star"], r["on_time"]
    # 已回复 6 轮：A1 A2 B C D F；A2 之后没有新来信（48 小时已过）→ 不算追问
    assert (fu["followed"], fu["not_followed"], fu["observing"]) == (1, 4, 1) and fu["rate"] == 0.2
    assert (esc["escalated"], esc["not_escalated"], esc["observing"]) == (1, 4, 1) and esc["rate"] == 0.2
    assert (ot["on_time"], ot["late"], ot["observing"]) == (5, 2, 1)  # 超时：D（30 小时）、E（没回）；G 观察中
    # 北极星：达标 A2、B；不达标 A1（追问）、C（升级）、D（超时）、E（超时没回）；观察中 F、G
    assert (ns["good"], ns["bad"], ns["observing"]) == (2, 4, 2) and ns["rate"] == round(2 / 6, 4)


def test_bands_night_backlog_and_volume():
    from app.round_metrics import build_rounds, compute

    now = bj(2026, 12, 1, 12)
    convs = [
        conv("W", [(bj(2026, 11, 10, 10), "BUYER"), (bj(2026, 11, 10, 10, 30), "SELLER")]),  # 工作时段 30 分钟
        conv("N1", [(bj(2026, 11, 10, 23, 30), "BUYER"), (bj(2026, 11, 11, 0, 30), "SELLER")]),  # 跨零点、夜里就回了：不算积压
        conv("N2", [(bj(2026, 11, 10, 20, 0), "BUYER"), (bj(2026, 11, 11, 9, 10), "SELLER")]),  # 次日上班才回：积压
        conv("S", [(bj(2026, 11, 14, 15, 0), "BUYER"), (bj(2026, 11, 16, 9, 30), "SELLER")]),  # 周六到、周一回：积压
        conv("P", [(bj(2026, 11, 29, 22, 0), "BUYER")], store="STORE_B"),  # 周日夜里到、还没回
    ]
    rounds = build_rounds(convs)
    buyer = [(c.store_id, m.at) for c in convs for m in c.msgs if m.role == "BUYER"]
    r = compute(rounds, {}, now, buyer, 14)
    bands = {b["key"]: b for b in r["bands"]}
    assert bands["work"]["median_seconds"] == 30 * 60 and bands["work"]["rounds"] == 1
    assert bands["night"]["rounds"] == 2 and bands["night"]["median_seconds"] == (H + (13 * H + 10 * 60)) // 2
    assert bands["weekend"]["rounds"] == 2 and bands["weekend"]["replied"] == 1
    nb = r["night_backlog"]
    assert (nb["count"], nb["pending"], nb["off_hours_rounds"]) == (2, 1, 4) and nb["rate"] == 0.5 and nb["per_week"] == 1.0
    v = r["volume"]
    assert v["total"] == 5 and {x["key"]: x["count"] for x in v["by_store"]} == {"STORE_A": 4, "STORE_B": 1}
    assert next(x for x in v["by_hour"] if x["hour"] == 23)["count"] == 1
    assert next(x for x in v["by_weekday"] if x["key"] == "周六")["count"] == 1 and v["per_day"] == round(5 / 14, 2)


def test_escalation_case_filter(client):
    from app.db import session_scope, utcnow
    from app.models import EbayCase
    from app.round_metrics import escalation_cases

    t = utcnow()
    with session_scope() as db:
        for i, (kind, reason, buyer) in enumerate([("RETURN", "DEFECTIVE_ITEM", "x1"), ("RETURN", "NO_LONGER_NEED_ITEM", "x2"),
                                                   ("RETURN", "DOES_NOT_FIT", "x3"), ("INQUIRY", None, "X4"),
                                                   ("PAYMENT_DISPUTE", "FRAUD", "x5"), ("PAYMENT_DISPUTE", "ITEM_NOT_RECEIVED", "x6"),
                                                   ("CANCEL", None, "x7"), ("FEEDBACK", None, "x8")]):
            db.add(EbayCase(store_id="STORE_A", kind=kind, external_id=str(i), buyer=buyer, reason=reason, opened_at=t))
        db.add(EbayCase(store_id="STORE_B", kind="CASE", external_id="9", buyer="x9", opened_at=t))
    with session_scope() as db:
        all_ = escalation_cases(db, None)
        assert {k[1] for k in all_} == {"x1", "x3", "x4", "x6", "x9"}  # 买家自己的原因、欺诈、取消、评价不算；买家名小写
        assert {k[1] for k in escalation_cases(db, ["STORE_A"])} == {"x1", "x3", "x4", "x6"}


def _recent_workday_10am(now):
    """3–7 天前最近的一个工作日，北京时间 10:00（UTC）：在近 7 天范围内，48 小时追问窗口也已经走完。"""
    for back in range(3, 8):
        d = (now + timedelta(hours=8) - timedelta(days=back)).date()
        if d.weekday() < 5:
            return bj(d.year, d.month, d.day, 10)
    raise AssertionError("找不到工作日")


def _seed_live(now):
    """上线后：店铺 A 一个会话——买家问 → 回复 → 买家“谢谢”（AI 判 thanks_closing，不算追问）。"""
    from app.db import session_scope
    from app.models import Conversation, Draft, Message, ReplyJob, ReplyJobSource

    with session_scope() as db:
        c = Conversation(store_id="STORE_A", ebay_conversation_id="L1", buyer_key="lb", conversation_type="FROM_MEMBERS")
        db.add(c)
        db.flush()
        t = _recent_workday_10am(now)  # 固定落在工作时段，不随跑测试的时刻变
        rows = [("L1-1", "BUYER", "IN", t, "Where is my order?"),
                ("L1-2", "SELLER", "OUT", t + timedelta(minutes=40), "It is on the way"),
                ("L1-3", "BUYER", "IN", t + timedelta(hours=4), "Perfekt, merci beaucoup und so")]
        for i, (mid, role, d, at, body) in enumerate(rows, 1):
            db.add(Message(store_id="STORE_A", ebay_message_id=mid, conversation_id=c.id, conversation_seq=i, sender_role=role,
                           direction=d, sent_at=at, body=body))
        j = ReplyJob(store_id="STORE_A", conversation_id=c.id, anchor_source_message_id="L1-3", state="REVIEW_REQUIRED")
        db.add(j)
        db.flush()
        db.add(ReplyJobSource(store_id="STORE_A", job_id=j.id, source_message_id="L1-3"))
        db.add(Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="x",
                     intent="thanks_closing"))


def _create_baseline(status="DONE"):
    """1 号的基线表（models.py）：一个完成的批次，店铺 A、B 各一个买家会话，外加一个系统通知会话（不算）。"""
    from app.db import session_scope
    from app.models import BaselineBatch, BaselineConversation, BaselineMessage

    t = bj(2026, 10, 13, 10)  # 周二工作时段
    with session_scope() as db:
        db.add(BaselineBatch(stores=["STORE_A", "STORE_B"], period_start=datetime(2026, 9, 10), period_end=datetime(2026, 11, 5),
                             status=status))
        convs = {}
        for key, store, buyer, ctype in (("B1", "STORE_A", "bb", "FROM_MEMBERS"), ("B2", "STORE_B", "bc", "FROM_MEMBERS"),
                                         ("B3", "STORE_A", "ebay", "FROM_EBAY")):
            c = BaselineConversation(store_id=store, ebay_conversation_id=key, buyer_key=buyer, conversation_type=ctype)
            db.add(c)
            db.flush()
            convs[key] = c
        msgs = [("B1", "BUYER", t, "Where is it?"), ("B1", "SELLER", t + timedelta(hours=4), "On the way"),
                ("B1", "BUYER", t + timedelta(hours=10), "Still not here?"), ("B1", "SELLER", t + timedelta(hours=14), "Checking"),
                ("B2", "BUYER", t, "Hello"), ("B2", "SELLER", t + timedelta(hours=2), "Hi"),
                ("B3", "BUYER", t, "system"),
                ("B1", "BUYER", datetime(2026, 9, 1), "too early")]  # 基线时间段外：不算
        for i, (key, role, at, body) in enumerate(msgs, 1):
            c = convs[key]
            db.add(BaselineMessage(store_id=c.store_id, conversation_id=c.id, ebay_message_id=f"BM{i}", sender_role=role,
                                   sent_at=at, body=body))


def test_baseline_missing_then_compare(client):
    from app.db import utcnow

    now = utcnow()
    _seed_live(now)
    start = (now - timedelta(days=7)).date().isoformat()
    from app.db import Base, get_engine

    # 线上库重启前还没有基线表：不报错，显示“基线未采集”
    with get_engine().begin() as conn:
        for t in ("baseline_messages", "baseline_conversations", "baseline_batches"):
            conn.execute(text(f"drop table {t}"))
    d = client.get("/api/performance/launch", params={"start": start}).json()
    assert d["baseline"]["available"] is False and d["baseline"]["status"] == "NONE" and d["baseline"]["data"] is None
    Base.metadata.create_all(get_engine())
    d = client.get("/api/performance/launch", params={"start": start}).json()  # 表在、没数据
    assert d["baseline"]["status"] == "NONE"
    rows = {x["key"]: x for x in d["baseline"]["comparison"]}
    assert rows["work_median"]["baseline_note"] == "基线未采集" and rows["follow_up"]["met"] is None
    assert rows["adoption"]["baseline_note"] == "无（纯人工时期没有记录）"
    q = d["quality"]
    assert q["follow_up"]["followed"] == 0 and q["follow_up"]["not_followed"] == 1  # “谢谢”按 AI 意图排除
    assert {"follow_up", "escalation", "north_star", "bands", "night_backlog", "baseline"} <= set(d["notes"])

    _create_baseline("RUNNING")  # 还在采集：显示“基线采集中”
    d = client.get("/api/performance/launch", params={"start": start}).json()
    assert d["baseline"]["status"] == "RUNNING" and d["baseline"]["comparison"][0]["baseline_note"] == "基线采集中"
    from app.db import session_scope
    from app.models import BaselineBatch

    with session_scope() as db:
        for x in db.query(BaselineBatch):
            x.status = "DONE"
    d = client.get("/api/performance/launch", params={"start": start}).json()
    b = d["baseline"]["data"]
    assert d["baseline"]["available"] and (b["period_start"], b["period_end"]) == ("2026-09-10", "2026-11-04")
    assert b["rounds"] == 3 and b["volume"]["total"] == 3  # 系统通知会话、时间段外的不算
    assert b["follow_up"]["followed"] == 1 and b["follow_up"]["rate"] == round(1 / 3, 4)  # 店铺 A 回复后 6 小时又问（3 轮已回复里 1 轮）
    bw = next(x for x in b["bands"] if x["key"] == "work")
    assert bw["median_seconds"] == 3 * H  # 工作时段：4 小时、2 小时（第二轮 20:00 到达算夜间）
    rows = {x["key"]: x for x in d["baseline"]["comparison"]}
    assert rows["work_median"]["baseline"] == 3 * H and rows["work_median"]["live"] == 40 * 60
    assert rows["work_median"]["met"] is True and rows["work_median"]["change"] == round((40 * 60 - 3 * H) / (3 * H), 4)
    assert rows["follow_up"]["met"] is True and rows["per_person_daily"]["baseline"] is None

    # 客服只看自己负责的店铺：基线也按店铺过滤
    make_user("cs_b", "CS", stores=["STORE_B"])
    cs = TestClient(client.app)
    login(cs, "cs_b")
    d = cs.get("/api/performance/launch", params={"start": start}).json()
    assert d["baseline"]["data"]["rounds"] == 1 and d["quality"]["rounds"] == 0


def test_pairing_first_seller_reply_and_windows_from_it():
    """同事常一问多答：首次响应配第一条店铺消息，连续多条店铺回复只算第一条；追问窗口也从第一条算起。"""
    from app.round_metrics import build_rounds, compute

    t0 = bj(2026, 11, 10, 10)
    c = conv("M", [(t0, "BUYER"), (t0 + timedelta(minutes=30), "SELLER"), (t0 + timedelta(hours=2), "SELLER"),
                   (t0 + timedelta(hours=40), "SELLER"),
                   (t0 + timedelta(minutes=30) + timedelta(hours=49), "BUYER", "And the second part?")])
    rounds = build_rounds([c])
    assert len(rounds) == 2 and rounds[0].reply_at == t0 + timedelta(minutes=30)
    r = compute(rounds[:1], {}, bj(2026, 12, 1, 10), [], 7)
    assert r["bands"][0]["median_seconds"] == 30 * 60
    assert r["follow_up"]["followed"] == 0  # 第一条回复后 49 小时才来信：窗口按第一条回复算，不算追问


def test_baseline_snapshot_survives_cleanup_and_recompute_rules(client, monkeypatch):
    from sqlalchemy import delete, select

    from app import launch_metrics, round_metrics
    from app.db import session_scope
    from app.models import BaselineMessage, BaselineSnapshot

    _create_baseline()
    d = client.get("/api/performance/launch").json()
    b = d["baseline"]["data"]
    assert b["source"] == "snapshot" and b["rounds"] == 3
    with session_scope() as db:
        snaps = {s.store_id: s for s in db.scalars(select(BaselineSnapshot))}
        assert set(snaps) == {"*", "STORE_A", "STORE_B"} and snaps["*"].batch_key == "1"
        assert snaps["*"].message_count == 6 and snaps["STORE_A"].metrics["rounds"] == 2
        assert "Where is it" not in str(snaps["*"].metrics)  # 不存买家正文
        first = snaps["*"].computed_at
    rows = {x["key"]: x for x in d["baseline"]["comparison"]}
    assert rows["work_median"]["baseline_sample"] == 2 and rows["work_median"]["small_sample"]
    assert rows["work_median"]["sample_note"] == "样本少，仅供参考"

    # 原始消息被清理（BASELINE_RETENTION_DAYS）：照样从快照出对比；口径版本变了也不拿残缺数据重算
    with session_scope() as db:
        db.execute(delete(BaselineMessage))
    monkeypatch.setattr(round_metrics, "RULES_VERSION", "next")
    d2 = client.get("/api/performance/launch").json()
    assert d2["baseline"]["data"]["rounds"] == 3 and d2["baseline"]["data"]["source"] == "snapshot"
    with session_scope() as db:
        assert db.scalar(select(BaselineSnapshot.computed_at).where(BaselineSnapshot.store_id == "*")) == first
        r = launch_metrics.refresh_baseline_snapshots(db, force=True)
        assert {x["action"] for x in r} == {"kept"} and all(x["reason"] == "原始消息已被清理，保留原快照" for x in r)


def test_baseline_snapshot_recomputed_when_rules_change_and_raw_complete(client, monkeypatch):
    from sqlalchemy import select

    from app import round_metrics
    from app.db import session_scope
    from app.models import BaselineSnapshot

    _create_baseline()
    client.get("/api/performance/launch")
    with session_scope() as db:
        first = db.scalar(select(BaselineSnapshot.computed_at).where(BaselineSnapshot.store_id == "*"))
    monkeypatch.setattr(round_metrics, "RULES_VERSION", "next")
    client.get("/api/performance/launch")
    with session_scope() as db:
        s = db.scalar(select(BaselineSnapshot).where(BaselineSnapshot.store_id == "*"))
        assert s.rules_version == "next" and s.computed_at >= first


def test_multi_store_scope_computes_from_raw(client):
    make_user("cs_ab", "CS", stores=["STORE_A", "STORE_B"])
    _create_baseline()
    cs = TestClient(client.app)
    login(cs, "cs_ab")
    b = cs.get("/api/performance/launch").json()["baseline"]["data"]
    assert b["source"] == "raw" and b["rounds"] == 3  # 两个店铺的组合没有现成快照：用原始消息现算


def test_cases_incomplete_note_and_recompute_after_backfill(client):
    """售后数据覆盖不到基线起点（最早售后晚于起点、没有基线补拉的单）：升级率、北极星标“售后数据不全”；
    补拉进售后单后快照自动重算、标注消失。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import BaselineSnapshot, EbayCase
    from app.round_metrics import case_coverage, cases_incomplete

    _create_baseline()
    with session_scope() as db:  # 店铺 A 最早的退货在 10-20（晚于 09-10 起点）；店铺 B 一张售后都没有
        db.add(EbayCase(store_id="STORE_A", kind="RETURN", external_id="R1", buyer="zz", reason="DEFECTIVE_ITEM",
                        opened_at=datetime(2026, 10, 20)))
    d = client.get("/api/performance/launch").json()
    assert set(d["baseline"]["data"]["cases_incomplete_stores"]) == {"STORE_A", "STORE_B"}
    rows = {x["key"]: x for x in d["baseline"]["comparison"]}
    assert rows["escalation"]["data_note"] == "售后数据不全，仅供参考" and rows["north_star"]["data_note"]
    assert rows["work_median"]["data_note"] is None
    with session_scope() as db:
        before = db.scalar(select(BaselineSnapshot.metrics).where(BaselineSnapshot.store_id == "*"))["cases_in_window"]

    # 1 号补拉：店铺 A、B 各补进一张基线期的退货（detail.origin=BASELINE_BACKFILL）
    with session_scope() as db:
        for store, ext, buyer in (("STORE_A", "R0", "bb"), ("STORE_B", "R2", "bc")):
            db.add(EbayCase(store_id=store, kind="RETURN", external_id=ext, buyer=buyer, reason="ARRIVED_DAMAGED",
                            opened_at=datetime(2026, 10, 14), detail={"origin": "BASELINE_BACKFILL"}))
    d = client.get("/api/performance/launch").json()
    assert d["baseline"]["data"]["cases_incomplete_stores"] == []
    assert {x["key"]: x for x in d["baseline"]["comparison"]}["escalation"]["data_note"] is None
    with session_scope() as db:
        assert db.scalar(select(BaselineSnapshot.metrics).where(BaselineSnapshot.store_id == "*"))["cases_in_window"] == before + 2
        cov = case_coverage(db, None, datetime(2026, 9, 10), datetime(2026, 11, 5))
    assert cases_incomplete(cov, ["STORE_A", "STORE_B", "STORE_C"], datetime(2026, 9, 10)) == ["STORE_C"]  # 没售后的店铺算不全


def test_baseline_refresh_endpoint_admin_only(client):
    _create_baseline()
    r = client.post("/api/performance/baseline/refresh").json()
    assert {x["store_id"] for x in r["results"]} == {"*", "STORE_A", "STORE_B"} and {x["action"] for x in r["results"]} == {"computed"}
    make_user("cs_r", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_r")
    assert cs.post("/api/performance/baseline/refresh").status_code == 403
