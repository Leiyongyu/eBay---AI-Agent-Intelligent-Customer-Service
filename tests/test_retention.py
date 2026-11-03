"""聊天记录保留期清理：按店铺保留最近 N 天，试运行只统计，未处理完的会话跳过，拉取时不再入库旧消息。"""
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 10, 26, 12, 0, 0)
OLD = NOW - timedelta(days=45)
RECENT = NOW - timedelta(days=3)


@pytest.fixture()
def rt(app_env, monkeypatch, tmp_path):
    from app import config
    from app.db import session_scope
    from app.models import Store

    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
    config.get_settings.cache_clear()
    with session_scope() as db:
        db.add(Store(id="RT", name="保留期测试店", default_marketplace_id="EBAY_DE"))
        db.add(Store(id="KEEP", name="不清理的店", default_marketplace_id="EBAY_DE", retention_days=0))
    return config.get_settings()


def conv(db, store, key, created=OLD):
    from app.models import Conversation

    c = Conversation(store_id=store, ebay_conversation_id=key, buyer_key="b-" + key, created_at=created)
    db.add(c)
    db.flush()
    return c


def msg(db, c, mid, at, seq):
    from app.models import Message

    m = Message(store_id=c.store_id, ebay_message_id=mid, conversation_id=c.id, conversation_seq=seq,
                sender_role="BUYER", direction="IN", sent_at=at, body=f"text {mid}")
    db.add(m)
    db.flush()
    return m


def image(db, m, settings, rel):
    from app.models import AttachmentStatus, MessageAttachment

    path = settings.media_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpg")
    db.add(MessageAttachment(store_id=m.store_id, conversation_id=m.conversation_id, message_id=m.id, seq=1,
                             media_type="IMAGE", source="ebay", status=AttachmentStatus.STORED, local_path=rel))
    return path


def job(db, c, state, created, anchor):
    from app.models import Draft, ReplyJob, ReplyJobSource

    j = ReplyJob(store_id=c.store_id, conversation_id=c.id, anchor_source_message_id=anchor, state=state,
                 created_at=created)
    db.add(j)
    db.flush()
    db.add(ReplyJobSource(store_id=c.store_id, source_message_id=anchor, job_id=j.id))
    db.add(Draft(job_id=j.id, run_generation=1, conversation_row_version=1, text="draft"))
    return j


def build(settings):
    """RT 店铺：
    mixed  —— 1 条旧消息（带图片）+ 1 条新消息：只删旧消息；
    old    —— 全是旧消息、旧任务已发送：整个会话删除；
    busy   —— 全是旧消息但任务待审核：整段跳过；
    KEEP 店铺（保留天数 0）—— 旧消息不动。"""
    from app.db import session_scope

    with session_scope() as db:
        mixed = conv(db, "RT", "mixed")
        m_old = msg(db, mixed, "MX1", OLD, 1)
        msg(db, mixed, "MX2", RECENT, 2)
        shared = image(db, m_old, settings, "RT/202609/a.jpg")
        old = conv(db, "RT", "old")
        o1 = msg(db, old, "OL1", OLD, 1)
        old_file = image(db, o1, settings, "RT/202609/b.jpg")
        job(db, old, "SENT", OLD, "OL1")
        busy = conv(db, "RT", "busy")
        msg(db, busy, "BS1", OLD, 1)
        job(db, busy, "REVIEW_REQUIRED", OLD, "BS1")
        keep = conv(db, "KEEP", "keep")
        msg(db, keep, "KP1", OLD, 1)
        return shared, old_file


def counts():
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Conversation, Draft, Message, MessageAttachment, ReplyJob

    with session_scope() as db:
        return {m.__tablename__: db.scalar(select(func.count()).select_from(m))
                for m in (Conversation, Message, MessageAttachment, ReplyJob, Draft)}


def test_dry_run_only_counts(rt):
    from app import retention

    build(rt)
    before = counts()
    r = retention.run(dry_run=True, now=NOW)
    assert r["ok"] and r["dry_run"]
    assert r["stores"]["RT"]["messages"] == 2  # MX1、OL1（busy 跳过）
    assert r["stores"]["RT"]["conversations"] == 1 and r["stores"]["RT"]["jobs"] == 1
    assert r["stores"]["RT"]["protected_conversations"] == 1
    assert r["stores"]["KEEP"]["skipped"]
    assert counts() == before


def test_cleanup_deletes_old_and_keeps_recent(rt):
    from sqlalchemy import select

    from app import retention
    from app.db import session_scope
    from app.models import AuditEvent, Conversation, Message

    shared, old_file = build(rt)
    r = retention.run(dry_run=False, now=NOW)
    t = r["stores"]["RT"]
    assert (t["messages"], t["attachments"], t["files"], t["jobs"], t["conversations"]) == (2, 2, 2, 1, 1)
    assert not shared.exists() and not old_file.exists()
    with session_scope() as db:
        left = {(c.ebay_conversation_id, m.ebay_message_id) for c, m in db.execute(
            select(Conversation, Message).join(Message, Message.conversation_id == Conversation.id))}
        assert left == {("mixed", "MX2"), ("busy", "BS1"), ("keep", "KP1")}
        assert db.scalar(select(AuditEvent.action).order_by(AuditEvent.id.desc()).limit(1)) == "RETENTION_CLEANUP"
    # 再跑一次没有可删的
    again = retention.run(dry_run=False, now=NOW)
    assert again["totals"]["messages"] == 0


def test_shared_image_file_kept_while_referenced(rt):
    from app import retention
    from app.db import session_scope

    build(rt)
    with session_scope() as db:  # 新消息引用同一张图片
        from sqlalchemy import select

        from app.models import Conversation

        c = db.scalar(select(Conversation).where(Conversation.ebay_conversation_id == "mixed"))
        m = msg(db, c, "MX3", RECENT, 3)
        path = image(db, m, rt, "RT/202609/a.jpg")
    retention.run(dry_run=False, now=NOW)
    assert path.exists()


def test_audit_retention(rt):
    from sqlalchemy import select

    from app import retention
    from app.db import session_scope
    from app.models import AuditEvent

    with session_scope() as db:
        db.add_all([AuditEvent(actor="ebay_sync", action="EBAY_SYNC", created_at=NOW - timedelta(days=40)),
                    AuditEvent(actor="ebay_sync", action="EBAY_SYNC", created_at=NOW - timedelta(days=5)),
                    AuditEvent(actor="tester", action="SEND_REQUESTED", created_at=NOW - timedelta(days=40)),
                    AuditEvent(actor="tester", action="SEND_REQUESTED", created_at=NOW - timedelta(days=200))])
    r = retention.run(dry_run=False, now=NOW)
    assert r["audit_events"] == {"sync": 1, "other": 1}
    with session_scope() as db:
        left = sorted((a.action, (NOW - a.created_at).days) for a in db.scalars(
            select(AuditEvent).where(AuditEvent.action.in_(["EBAY_SYNC", "SEND_REQUESTED"]))))
    assert left == [("EBAY_SYNC", 5), ("SEND_REQUESTED", 40)]


def test_schedule_due_time():
    from app.retention import last_due, parse_clock

    at = parse_clock("03:30")
    assert last_due(datetime(2026, 10, 26, 2, 0), at) == datetime(2026, 10, 25, 3, 30)
    assert last_due(datetime(2026, 10, 26, 4, 0), at) == datetime(2026, 10, 26, 3, 30)
    assert parse_clock("bad") == at


def test_api_status_and_manual_run(rt, client):
    build(rt)
    s = client.get("/internal/maintenance/retention").json()
    assert s["days"] == 30 and s["dry_run"] is True
    assert {x["id"]: x["effective_days"] for x in s["stores"]}["KEEP"] == 0
    r = client.post("/internal/maintenance/retention/run", json={"dry_run": True})
    assert r.status_code == 200 and r.json()["totals"]["messages"] == 2
    assert client.get("/internal/maintenance/retention").json()["runs"][0]["actor"] == "tester"
    from fastapi.testclient import TestClient

    anonymous = TestClient(client.app)  # 没登录：401
    assert anonymous.post("/internal/maintenance/retention/run", json={"dry_run": True}).status_code == 401


def test_sync_skips_messages_older_than_cutoff(app_env, monkeypatch):
    from sqlalchemy import select

    from app import config
    from app.db import session_scope
    from app.ebay import sync as sync_mod
    from app.models import Conversation, Message
    from test_ebay_sync import DAY, FakeEbay, make_sync

    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-id")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "v^1.1#i^1#r^1#fake-refresh")
    monkeypatch.setenv("EBAY_STORE_ID", "REAL_US")
    config.get_settings.cache_clear()
    cut = datetime.combine(DAY, datetime.min.time()).replace(hour=10, minute=15)  # 样例消息在前天 08:00–11:00 之间
    monkeypatch.setattr(sync_mod, "store_cutoff", lambda db, store_id: cut)
    monkeypatch.setattr(sync_mod, "cutoff_for", lambda store, now=None: cut)

    r = make_sync(FakeEbay()).run()
    members = r["types"]["FROM_MEMBERS"]
    assert members["messages_new"] == 2 and members["messages_skipped_old"] == 3
    assert members["conversations_skipped_old"] == 2  # C1、C2 全是旧消息：不建会话
    with session_scope() as db:
        convs = set(db.scalars(select(Conversation.ebay_conversation_id).where(Conversation.store_id == "REAL_US")))
        assert convs == {"C3", "C4", "E1"}
        assert all(t >= cut for t in db.scalars(select(Message.sent_at).where(Message.store_id == "REAL_US")))
