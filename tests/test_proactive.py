"""主动发信：规则默认关、阈值校验、四条规则的候选、跳过原因（没会话 / 黑名单 / 已有待处理回复 / 7 天内触发过）、
生成的草稿进“未处理”、工作台照常审核发送（走发件箱）、作废、定时开关。只读本地数据，不连外部系统。"""
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def world(app_env):
    from app.db import session_scope
    from app.models import Conversation, EbayCase, EbayOrder, Message, Store, TrackingNumber

    n = NOW.replace(tzinfo=None)
    with session_scope() as db:
        db.add(Store(id="PS", name="主动店", timezone="Europe/Berlin", default_marketplace_id="EBAY_DE"))
        db.flush()
        ids = {}
        for key, buyer, mk, text in (("stall", "b_stall", "EBAY_DE", "Hallo, danke für die schnelle Lieferung bisher"),
                                     ("pickup", "b_pick", "EBAY_GB", "Thanks"),
                                     ("ret", "b_ret", "EBAY_FR", "Bonjour, je veux retourner l'article"),
                                     ("inq", "b_inq", "EBAY_IT", "Il pacco non è ancora arrivato")):
            c = Conversation(store_id="PS", ebay_conversation_id=f"C-{key}", buyer_key=buyer, marketplace_id=mk,
                             last_message_at=n - timedelta(days=2))
            db.add(c)
            db.flush()
            db.add(Message(store_id="PS", ebay_message_id=f"M-{key}", conversation_id=c.id, conversation_seq=1, sender_role="BUYER",
                           direction="IN", body=text, sent_at=n - timedelta(days=2)))
            db.add(Message(store_id="PS", ebay_message_id=f"S-{key}", conversation_id=c.id, conversation_seq=2, sender_role="SELLER",
                           direction="OUT", body="ok", sent_at=n - timedelta(days=2)))
            ids[key] = c.id
        for oid, buyer in (("26-00001-00001", "b_stall"), ("26-00001-00002", "b_pick"), ("26-00001-00003", "b_nobody")):
            db.add(EbayOrder(store_id="PS", order_id=oid, buyer=buyer, created_at=n - timedelta(days=12)))
        db.add(TrackingNumber(number="00340434160000000001", order_id="26-00001-00001", carrier_name="DHL", status="InTransit",
                              track={"status": "InTransit", "events": [{"time_utc": _iso(NOW - timedelta(days=6)), "description": "x"}]}))
        db.add(TrackingNumber(number="RM111111111GB", order_id="26-00001-00002", carrier_name="Royal Mail", status="AvailableForPickup",
                              track={"status": "AvailableForPickup", "events": [{"time_utc": _iso(NOW - timedelta(days=9)), "description": "x"}]}))
        db.add(TrackingNumber(number="00340434160000000003", order_id="26-00001-00003", carrier_name="DHL", status="InTransit",
                              track={"status": "InTransit", "events": [{"time_utc": _iso(NOW - timedelta(days=8)), "description": "x"}]}))
        db.add(EbayCase(store_id="PS", kind="RETURN", external_id="5300777", buyer="b_ret", status="RETURN_ACCEPTED/WAITING_FOR_RETURN_SHIPMENT",
                        is_open=True, status_changed_at=n - timedelta(days=8)))
        db.add(EbayCase(store_id="PS", kind="INQUIRY", external_id="5390777", buyer="b_inq", status="OPEN", is_open=True,
                        due_at=n + timedelta(hours=10)))
        db.add(EbayCase(store_id="PS", kind="INQUIRY", external_id="5390778", buyer="b_inq", status="OPEN", is_open=True,
                        due_at=n + timedelta(hours=10), work_state="PROCESSING"))  # 已在处理：不提醒
    return ids


def _enable(db, **th):
    from app import proactive

    proactive.set_rules(db, {k: {"enabled": True, "threshold": th.get(k, v["threshold"])}
                             for k, v in proactive.get_rules(db).items()}, "tester")


def test_defaults_off_and_validation(app_env):
    from app import proactive
    from app.db import session_scope

    with session_scope() as db:
        assert all(not r["enabled"] for r in proactive.get_rules(db).values())
        assert proactive.scan(db, now=NOW) == {}  # 都关着：什么都不扫
        with pytest.raises(ValueError):
            proactive.set_rules(db, {"tracking_stalled": {"threshold": 0}}, "t")
        with pytest.raises(ValueError):
            proactive.set_rules(db, {"nope": {"enabled": True}}, "t")


def test_scan_drafts_into_pending(world):
    from sqlalchemy import select

    from app import proactive
    from app.db import session_scope
    from app.models import Draft, JobState, ReplyJob

    with session_scope() as db:
        _enable(db, pickup_overdue=7)
        dry = proactive.scan(db, dry_run=True, now=NOW)
        assert {k: v["candidates"] for k, v in dry.items()} == {"tracking_stalled": 2, "pickup_overdue": 1,
                                                                 "return_not_shipped": 1, "case_due_soon": 1}
        assert dry["tracking_stalled"]["skipped"] == {"对不上买家会话": 1}  # 第三张订单的买家没有会话
        assert not db.scalars(select(ReplyJob)).all()  # 预览不建任务
        res = proactive.scan(db, now=NOW)
        assert {k: v["drafted"] for k, v in res.items()} == {"tracking_stalled": 1, "pickup_overdue": 1,
                                                             "return_not_shipped": 1, "case_due_soon": 1}
        jobs = {j.conversation_id: j for j in db.scalars(select(ReplyJob))}
        stall = jobs[world["stall"]]
        assert stall.state == JobState.REVIEW_REQUIRED and stall.decision_reasons == ["PROACTIVE_NOTICE"]
        d = db.scalars(select(Draft).where(Draft.job_id == stall.id)).one()
        import re

        # 断更天数按 LOGISTICS_SKIP_SUNDAYS 不算周日：日历上 6 天，算出来 5 天多
        assert d.author == "proactive" and d.language == "de" and "00340434160000000001" in d.text
        m = re.search(r"seit (\d+) Tagen", d.text)
        assert m and int(m.group(1)) >= 5 and f"{m.group(1)} 天没有更新" in d.text_zh
        texts = {cid: db.scalars(select(Draft.text).where(Draft.job_id == j.id)).one() for cid, j in jobs.items()}
        assert "pickup point" in texts[world["pickup"]] and re.search(r"for \d+ days", texts[world["pickup"]])
        assert "5300777" in texts[world["ret"]] and texts[world["ret"]].startswith("Bonjour")
        assert texts[world["inq"]].startswith("Buongiorno") and "5390777" in texts[world["inq"]]
        again = proactive.scan(db, now=NOW)  # 已经有待处理的回复：不再触发
        assert again["tracking_stalled"]["drafted"] == 0 and again["tracking_stalled"]["skipped"].get("会话里已经有待处理的回复") == 1
        proactive.dismiss(db, stall.id, "tester")
        assert stall.state == JobState.CANCELLED_STALE
        third = proactive.scan(db, now=NOW)  # 作废了，但 7 天内触发过
        assert third["tracking_stalled"]["skipped"].get("7 天内已经触发过") == 1


def test_blacklisted_buyer_skipped(world):
    from app import proactive
    from app.db import session_scope
    from app.models import BuyerBlacklist

    with session_scope() as db:
        db.add(BuyerBlacklist(store_id="PS", buyer="b_ret", reason="t", created_by="t"))
        db.flush()
        _enable(db)
        res = proactive.scan(db, dry_run=True, now=NOW, rules=["return_not_shipped"])
        assert res["return_not_shipped"]["skipped"] == {"黑名单买家": 1}


def test_draft_sent_through_workbench(world, client):
    from app import proactive
    from app.db import session_scope

    with session_scope() as db:
        _enable(db)
        proactive.scan(db, now=NOW, rules=["case_due_soon"])
    d = client.get(f"/internal/conversations/{world['inq']}").json()
    assert d["job"]["state"] == "REVIEW_REQUIRED" and d["drafts"][0]["author"] == "proactive"
    text = d["drafts"][0]["text"]
    r = client.post(f"/internal/conversations/{world['inq']}/send", json={"text": text})
    assert r.status_code == 200, r.text
    d = client.get(f"/internal/conversations/{world['inq']}").json()
    assert d["job"]["state"] == "SENT" and d["messages"][-1]["role"] == "SELLER"  # 测试里发送延迟为 0
    n = client.get("/internal/proactive/notices").json()
    assert n[0]["rule"] == "case_due_soon" and n[0]["state"] == "SENT"


def test_api_rules_and_scan(world, client):
    r = client.get("/internal/proactive/rules").json()
    assert [x["key"] for x in r["rules"]] == ["tracking_stalled", "pickup_overdue", "return_not_shipped", "case_due_soon"]
    assert client.put("/internal/proactive/rules", json={"rules": {"case_due_soon": {"enabled": True, "threshold": 48}}}).status_code == 200
    assert client.get("/internal/proactive/rules").json()["rules"][3] == {"key": "case_due_soon", "label": "咨询 / 纠纷快到期未处理",
                                                                         "unit": "小时", "enabled": True, "threshold": 48.0}
    prev = client.post("/internal/proactive/scan?dry_run=true").json()
    assert list(prev) == ["case_due_soon"] and prev["case_due_soon"]["drafted"] == 0
    assert client.post("/internal/proactive/scan?dry_run=true&rule=nope").status_code == 422
    assert client.put("/internal/proactive/rules", json={"rules": {"case_due_soon": {"threshold": 9999}}}).status_code == 422


def test_worker_switch(app_env, monkeypatch):
    from app import config, proactive

    monkeypatch.setenv("PROACTIVE_SCAN_MINUTES", "0")
    config.get_settings.cache_clear()
    assert proactive.ProactiveWorker().start() is False
    config.get_settings.cache_clear()
