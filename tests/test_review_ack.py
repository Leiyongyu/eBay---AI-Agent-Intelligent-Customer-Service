"""转人工的来信超时没人回复，给买家发“已收到”回执（REVIEW_ACK_ENABLED）。
2026-11-09 起按用户要求默认关闭：没人回复的来信一律保持待审核，系统不替人发。下面的用例显式打开开关，验证打开时的行为。"""
from datetime import timedelta

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _ack_on(request, monkeypatch, app_env):
    """原有用例按“打开回执”验证；test_default_off_* 保持默认（关闭）。"""
    if not request.node.name.startswith("test_default_off"):
        monkeypatch.setattr(get_settings(), "review_ack_enabled", True)


def test_default_off_sends_nothing(app_env):
    """默认关闭：回执任务不启动，run() 什么都不发、不产生发件箱记录，买家消息保持待审核（含只是致谢的）。"""
    from sqlalchemy import func, select

    from app import review_ack
    from app.db import session_scope
    from app.models import OutboundMessage

    assert get_settings().review_ack_enabled is False
    assert review_ack.ReviewAckWorker().start() is False
    with session_scope() as db:
        conv, job = _setup(db, "OFF1")
        conv2, job2 = _setup(db, "OFF2", intents=("thanks_closing",))  # 只是致谢：原来会自动回简短致谢
        assert review_ack.run(db) == {}
        assert job.ack_status is None and job2.ack_status is None and job.state == "REVIEW_REQUIRED"
        assert _sellers(db, conv.id) == [] and _sellers(db, conv2.id) == []
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def _setup(db, key, *, site="EBAY_DE", minutes_ago=40, intents=("fitment",), language="de", state="REVIEW_REQUIRED"):
    from app.db import utcnow
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.models import Draft, ReplyJob

    conv = get_or_create_conversation(db, "STORE_A", f"C-ACK{key}", f"buyer_ack{key}", None, site, None)
    asked = utcnow() - timedelta(minutes=minutes_ago)
    m = _append_message(db, conv, f"MACK{key}", "BUYER", "IN", "Passt das auf meinen Golf?", asked)
    job = ReplyJob(store_id="STORE_A", conversation_id=conv.id, anchor_source_message_id=m.ebay_message_id, state=state,
                   created_at=asked)
    db.add(job)
    db.flush()
    db.add(Draft(job_id=job.id, run_generation=1, conversation_row_version=conv.row_version, text="Entwurf",
                 language=language, intents=list(intents)))
    db.flush()
    return conv, job


def _sellers(db, conv_id):
    from sqlalchemy import select

    from app.models import Message

    return db.scalars(select(Message).where(Message.conversation_id == conv_id, Message.sender_role == "SELLER")
                      .order_by(Message.id)).all()


def test_ack_after_delay_once_in_buyer_language(app_env):
    from app import review_ack
    from app.db import session_scope
    from app.sending.service import auto_reply_streak, is_ack

    with session_scope() as db:
        conv, job = _setup(db, "1")
        fresh_conv, fresh = _setup(db, "2", minutes_ago=5)  # 还没到 30 分钟
        old_conv, old = _setup(db, "3", minutes_ago=60 * 30)  # 超过 24 小时：不补发
        thanks_conv, thanks = _setup(db, "4", intents=("thanks_closing",))  # 只是致谢
        fr_conv, fr = _setup(db, "5", site="EBAY_FR", language=None)  # 草稿没写语言：按站点
        stats = review_ack.run(db)
        assert stats["SENT"] == 3 and stats["WAIT"] == 1
        msg = _sellers(db, conv.id)[0]
        assert msg.body.startswith("Vielen Dank") and msg.zh_translation.startswith("感谢") and is_ack(msg.ebay_message_id)
        assert (job.ack_status, job.ack_message_id) == ("SENT", msg.id) and job.state == "REVIEW_REQUIRED"  # 草稿仍待审
        assert _sellers(db, fr_conv.id)[0].body.startswith("Merci")
        assert not _sellers(db, fresh_conv.id) and fresh.ack_status is None
        assert not _sellers(db, old_conv.id) and old.ack_status is None  # 超过 24 小时的根本不扫
        # 规则：最后一句永远是我们的——只是致谢也要回，回简短致谢（不回“已收到，正在处理”）
        closing = _sellers(db, thanks_conv.id)[0]
        assert closing.body == review_ack.CLOSING_TEXT["de"] and thanks.ack_status == "SENT"
        assert review_ack.run(db).get("SENT") is None  # 不重复发
        assert auto_reply_streak(db, conv.id) == 0  # 回执不算自动回复
        conv_row_version = conv.row_version
    with session_scope() as db:
        from app.models import Conversation

        assert db.get(Conversation, conv.id).row_version == conv_row_version  # 不让待审草稿过期


def test_ack_skips_when_replied_claimed_or_recently_acked(app_env):
    from app import review_ack
    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message
    from app.models import JobState, WorkClaim

    with session_scope() as db:
        conv, job = _setup(db, "R")
        _append_message(db, conv, "SIM-OUT-manual1", "SELLER", "OUT", "Hallo, ja das passt.", utcnow() - timedelta(minutes=10))
        claimed_conv, claimed = _setup(db, "C")
        db.add(WorkClaim(store_id="STORE_A", conversation_id=claimed_conv.id, claimed_by="tester",
                         claimed_at=utcnow(), expires_at=utcnow() + timedelta(minutes=10)))
        db.flush()
        review_ack.run(db)
        assert job.ack_status == "不需要：来信后已有回复"
        assert claimed.ack_status is None and not _sellers(db, claimed_conv.id)  # 同事在处理：先不发

        # 发过回执、还没正式回复，买家接着追问（新任务）：12 小时内不重复发
        conv2, job2 = _setup(db, "F", minutes_ago=120)
        review_ack.run(db)
        assert job2.ack_status == "SENT"
        _sellers(db, conv2.id)[0].sent_at = utcnow() - timedelta(minutes=90)  # 回执在追问之前
        job2.state = JobState.CANCELLED_STALE
        m = _append_message(db, conv2, "MACKF2", "BUYER", "IN", "Hallo??", utcnow() - timedelta(minutes=40))
        from app.models import ReplyJob

        job3 = ReplyJob(store_id="STORE_A", conversation_id=conv2.id, anchor_source_message_id=m.ebay_message_id,
                        state=JobState.REVIEW_REQUIRED, created_at=utcnow() - timedelta(minutes=40))
        db.add(job3)
        db.flush()
        review_ack.run(db)
        assert job3.ack_status == "不需要：刚发过回执，还没正式回复" and len(_sellers(db, conv2.id)) == 1


def test_ack_disabled_and_texts_pass_compliance(app_env, monkeypatch):
    from app import review_ack
    from app.db import session_scope
    from app.guards.compliance import ComplianceChecker

    for lang, text in review_ack.ACK_TEXT.items():
        assert ComplianceChecker().check(text).result != "BLOCK", lang
    monkeypatch.setattr(get_settings(), "review_ack_enabled", False)
    with session_scope() as db:
        conv, job = _setup(db, "D")
        assert review_ack.run(db) == {} and job.ack_status is None


def test_generate_draft_ignores_ack(client):
    """回执不算店铺回复：工作台“生成草稿”仍能覆盖回执之前的买家消息。"""
    from app.db import session_scope
    from app import review_ack

    with session_scope() as db:
        conv, job = _setup(db, "G")
        review_ack.run(db)
        conv_id = conv.id
    detail = client.get(f"/internal/conversations/{conv_id}").json()
    assert [m["ack"] for m in detail["messages"] if m["role"] == "SELLER"] == [True]
    r = client.post(f"/internal/conversations/{conv_id}/generate-draft")
    assert r.status_code != 409 or "没有待回复" not in r.text


def test_ack_also_for_human_handoff(app_env):
    """已人工接管的会话：同事超时没回，买家也要先收到“已收到”（最后一句永远是我们的）；黑名单买家仍不发。"""
    from app import review_ack
    from app.db import session_scope

    with session_scope() as db:
        conv, job = _setup(db, "H")
        conv.handoff_state = "HUMAN"
        db.flush()
        assert review_ack.run(db).get("SENT") == 1
        assert _sellers(db, conv.id)[0].body.startswith("Vielen Dank") and job.ack_status == "SENT"
    for lang, text in review_ack.CLOSING_TEXT.items():
        from app.guards.compliance import ComplianceChecker

        assert ComplianceChecker().check(text).result != "BLOCK", lang
