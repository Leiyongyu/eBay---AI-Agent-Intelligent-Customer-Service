"""测试期的删除会话：开关关闭时不可用；只能删测试会话，eBay 真实会话不能删；AI 正在处理的不删。"""
import pytest
from sqlalchemy import func, select

from app import config


def make(db, key, source=None, job_state=None):
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import ReplyJob

    conv = get_or_create_conversation(db, "STORE_A", key, "buyer_" + key, None, "EBAY_US", "simulator")
    conv.channel_source = source
    m = _append_message(db, conv, f"SIM-IN-{key}", "BUYER", "IN", "hello", None)
    if job_state:
        db.add(ReplyJob(store_id="STORE_A", conversation_id=conv.id, anchor_source_message_id=m.ebay_message_id,
                        state=job_state))
    db.flush()
    return conv.id


@pytest.fixture()
def convs(client):
    from app.db import session_scope
    from app.models import JobState

    with session_scope() as db:
        return {"t1": make(db, "t1", job_state=JobState.REVIEW_REQUIRED), "t2": make(db, "t2"),
                "busy": make(db, "busy", job_state=JobState.RUNNING), "real": make(db, "real", source="ebay")}


def counts():
    from app.db import session_scope
    from app.models import Conversation, Message, ReplyJob

    with session_scope() as db:
        return {m.__name__: db.scalar(select(func.count()).select_from(m)) for m in (Conversation, Message, ReplyJob)}


def test_disabled_by_default(client, convs):
    assert client.delete(f"/internal/conversations/{convs['t2']}").status_code == 403
    assert client.get("/api/system/meta").json()["test_delete_enabled"] is False


def test_delete_one_and_purge(client, convs, monkeypatch):
    monkeypatch.setenv("TEST_DELETE_ENABLED", "true")
    config.get_settings.cache_clear()
    before = counts()
    r = client.delete(f"/internal/conversations/{convs['t1']}")
    assert r.status_code == 200 and (r.json()["conversations"], r.json()["messages"], r.json()["jobs"]) == (1, 1, 1)
    assert client.get(f"/internal/conversations/{convs['t1']}").status_code == 404
    assert client.delete(f"/internal/conversations/{convs['real']}").status_code == 409  # eBay 真实会话不能删
    assert client.delete(f"/internal/conversations/{convs['busy']}").status_code == 409  # AI 正在处理

    r = client.post("/internal/conversations/purge-test", json={})
    assert r.json()["skipped_busy"] == 1 and r.json()["conversations"] >= 1
    from app.db import session_scope
    from app.models import Conversation

    with session_scope() as db:
        left = set(db.scalars(select(Conversation.id)))
    assert convs["real"] in left and convs["busy"] in left and convs["t2"] not in left
    assert counts()["Conversation"] < before["Conversation"]
