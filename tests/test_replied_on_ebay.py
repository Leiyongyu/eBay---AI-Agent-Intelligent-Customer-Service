"""同事直接在 eBay 后台回复了买家：这个会话里还没发出的任务和 AI 草稿作废（不发任何消息）。"""
import copy
from datetime import timedelta

from sqlalchemy import func, select

from app import config
from test_ebay_sync import CONVS, MESSAGES, SELLER, FakeEbay, _msg, _ts, ebay_env, make_sync  # noqa: F401  ebay_env 是夹具
from test_pipeline import draft_json, send_buyer, use_fake_llm


def _jobs(db, conv_ebay_id):
    from sqlalchemy import select

    from app.models import Conversation, ReplyJob

    conv = db.scalar(select(Conversation).where(Conversation.ebay_conversation_id == conv_ebay_id))
    return conv, db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv.id).order_by(ReplyJob.id)).all()


def test_sync_closes_pending_job_when_replied_on_ebay(ebay_env, monkeypatch):
    """C2：买家 10:00 来信 → 待审核（带 AI 草稿）；同事 10:20 在 eBay 后台回复 → 任务关掉、草稿作废、原因 REPLIED_ON_EBAY。
    C3：本系统发出的回复同步回来不触发。C4：店铺回复之后买家又来信 → 旧任务关掉，新来信照常建任务。发件箱待发消息不动。"""
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import (AuditEvent, ConversationEvent, Draft, JobState, Message, OutboundMessage, Store)

    from app.models import Conversation, ReplyJob

    assert make_sync(FakeEbay()).run()["ok"]
    with session_scope() as db:
        db.get(Store, "REAL_US").auto_send_enabled = True
    with session_scope() as db:  # 删掉已入库的，模拟开关打开后才拉到这些会话
        for t in (ReplyJob, Message, Conversation):
            db.execute(t.__table__.delete())
    assert make_sync(FakeEbay()).run()["ok"]  # 最后一句是买家：C2、C3、C4 各补建一个任务
    with session_scope() as db:
        c2, (j2,) = _jobs(db, "C2")
        j2.state = JobState.REVIEW_REQUIRED
        db.add(Draft(job_id=j2.id, run_generation=1, conversation_row_version=c2.row_version, text="Ihr Paket ...",
                     language="de", intents=["shipping_status"]))
        c3, (j3,) = _jobs(db, "C3")
        # 本系统刚发出的消息（发件箱已记下 eBay 消息 ID），同步时才第一次拉到
        db.add(OutboundMessage(store_id=c3.store_id, conversation_id=c3.id, body="Bonjour !", source="manual",
                               actor="tester", state="SENT", platform_message_id="M4-OWN", send_after=c3.created_at))
        # C2 发件箱里还有一条撤回期内的待发消息：不能被动
        db.add(OutboundMessage(store_id=c2.store_id, conversation_id=c2.id, body="queued", source="manual", actor="tester",
                               state="PENDING", send_after=c2.created_at))
        sent_before = db.scalar(select(func.count()).select_from(Message).where(Message.sender_role == "SELLER"))

    convs, msgs = copy.deepcopy(CONVS), copy.deepcopy(MESSAGES)
    msgs["C2"].append(_msg("M3-EBAY", SELLER, "buyer_b", "Wir erstatten die Hälfte, Sie behalten den Artikel.", _ts("10:20")))
    msgs["C3"].append(_msg("M4-OWN", SELLER, "buyer_c", "Bonjour !", _ts("10:35")))
    msgs["C4"] += [_msg("M5-EBAY", SELLER, "buyer_d", "Yes, available.", _ts("10:50")),
                   _msg("M5-AGAIN", "buyer_d", SELLER, "Great, can you ship today?", _ts("11:00"))]
    for i, cid in ((1, "C2"), (2, "C3"), (3, "C4")):
        convs["FROM_MEMBERS"][i]["latestMessage"] = msgs[cid][-1]
    monkeypatch.setattr("test_ebay_sync.CONVS", convs)
    monkeypatch.setattr("test_ebay_sync.MESSAGES", msgs)
    stats = make_sync(FakeEbay()).run()
    assert stats["ok"]

    with session_scope() as db:
        _, (j2,) = _jobs(db, "C2")
        assert j2.state == JobState.RESOLVED_NO_REPLY and "REPLIED_ON_EBAY" in j2.decision_reasons
        assert db.scalars(select(Draft.status).where(Draft.job_id == j2.id)).all() == ["SUPERSEDED"]
        ev = db.scalar(select(ConversationEvent).where(ConversationEvent.conversation_id == j2.conversation_id,
                                                       ConversationEvent.event_type == "REPLIED_ON_EBAY"))
        assert ev is not None and "已在 eBay 后台回复" in ev.detail
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "JOB_CLOSED_REPLIED_ON_EBAY")) is not None
        pending = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_id == j2.conversation_id,
                                                          OutboundMessage.state == "PENDING"))
        assert pending is not None  # 发件箱待发消息不动

        _, (j3,) = _jobs(db, "C3")
        assert j3.state == JobState.RECEIVED  # 本系统发出的：不触发

        c4, jobs4 = _jobs(db, "C4")
        assert [j.state for j in jobs4] == [JobState.RESOLVED_NO_REPLY, JobState.RECEIVED]  # 旧的关掉，新来信照常建
        assert jobs4[1].anchor_source_message_id == "M5-AGAIN"
        # 整个过程没给买家发消息：新增的店铺消息只有同步拉到的 3 条
        assert db.scalar(select(func.count()).select_from(Message).where(Message.sender_role == "SELLER")) == sent_before + 3


def test_running_job_closed_at_persist_when_replied_meanwhile(client):
    """生成中（RUNNING）的任务：落库前发现同事已在 eBay 后台回复（晚于它最后一条买家消息）→ 不进待审核、草稿不落库。"""
    from sqlalchemy import select

    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message
    from app.models import Conversation, Draft, JobState, ReplyJob
    from app.worker import worker

    use_fake_llm(draft_json())
    cid = send_buyer(client, "Where is my parcel?")["conversation_id"]
    with session_scope() as db:
        conv = db.get(Conversation, cid)
        _append_message(db, conv, "EBAY-BACKOFFICE-1", "SELLER", "OUT", "Shipped today, tracking 123.", utcnow())
    worker.run_once()
    with session_scope() as db:
        job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid))
        assert job.state == JobState.RESOLVED_NO_REPLY and "REPLIED_ON_EBAY" in job.decision_reasons
        assert db.scalars(select(Draft).where(Draft.job_id == job.id, Draft.status == "ACTIVE")).all() == []


def test_own_messages_and_buyer_after_reply_not_closed(client):
    """本系统发出的（SIM-OUT 前缀、发送记录里的 eBay 消息 ID）不算后台回复；买家在回复之后又来信的任务不关。"""
    from datetime import timedelta

    from sqlalchemy import select

    from app import ebay_replied
    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message
    from app.models import Conversation, JobState, ReplyJob

    cid = send_buyer(client, "Is it in stock?")["conversation_id"]
    with session_scope() as db:
        conv = db.get(Conversation, cid)
        job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid))
        job.state = JobState.REVIEW_REQUIRED
        own = _append_message(db, conv, "SIM-OUT-abc", "SELLER", "OUT", "Yes.", utcnow())
        assert ebay_replied.close_replied_jobs(db, conv, own) == []
        early = _append_message(db, conv, "EBAY-OLD", "SELLER", "OUT", "Hello", utcnow() - timedelta(days=1))
        assert ebay_replied.close_replied_jobs(db, conv, early) == []  # 早于买家这条来信：不是在回它
        assert job.state == JobState.REVIEW_REQUIRED
        assert ebay_replied.stale_pending(db) == []
        late = _append_message(db, conv, "EBAY-NEW", "SELLER", "OUT", "Yes, 3 left.", utcnow() + timedelta(minutes=1))
        rows = ebay_replied.stale_pending(db)  # 存量预览：只列出、不改
        assert [r["job_id"] for r in rows] == [job.id] and job.state == JobState.REVIEW_REQUIRED
        assert ebay_replied.close_replied_jobs(db, conv, late) == [job.id]


def test_reason_label():
    from app.guards.risk import REASON_LABELS

    assert REASON_LABELS["REPLIED_ON_EBAY"] == "同事已在 eBay 后台回复，系统草稿作废"
    assert config.get_settings() is not None


# ---------------------------------------------------------------- 发送结果不确定但其实发出去了（问题 H）
def _uncertain_case(client, buyer, body="Thanks, it is in stock.\n\nBest regards"):
    from test_replied_on_ebay_acceptance import _outbox, _setup

    from app.db import session_scope
    from app.models import Draft, ReplyJob, SendIntent

    cid, j1, at = _setup(client, buyer=buyer)
    with session_scope() as db:
        db.get(ReplyJob, j1).state = "SEND_UNCERTAIN"
        d = db.scalar(select(Draft).where(Draft.job_id == j1))
        db.add(SendIntent(job_id=j1, draft_id=d.id, text=body, content_hash="x" * 64, idempotency_key=f"h-{buyer}",
                          state="SEND_UNCERTAIN", actor="tester"))
    _outbox(cid, "UNCERTAIN", None, body=body, job_id=j1)
    _, j2, at2 = _setup(client, text="Also, can you ship today?", buyer=buyer)
    return cid, j1, j2, at2


def test_uncertain_send_confirmed_by_sync(client):
    from test_replied_on_ebay_acceptance import _reply, _state

    from app.db import session_scope
    from app.models import AuditEvent, Message, OutboundMessage, SendIntent

    cid, j1, j2, at2 = _uncertain_case(client, "buyer_h1")
    # eBay 返回的正文换行、空白不一样也算同一条
    assert _reply(cid, "EBAY-REAL-H1", at2 + timedelta(minutes=2), body="Thanks, it is in stock.  Best regards\n") == []
    assert _state(j2)[0] == "REVIEW_REQUIRED" and _state(j1)[0] == "SENT"
    with session_scope() as db:
        ob = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_id == cid))
        msg = db.scalar(select(Message).where(Message.ebay_message_id == "EBAY-REAL-H1"))
        assert (ob.state, ob.platform_message_id, ob.message_id, ob.error) == ("SENT", "EBAY-REAL-H1", msg.id, None)
        intent = db.scalar(select(SendIntent).where(SendIntent.job_id == j1))
        assert (intent.state, intent.platform_message_id) == ("SENT", "EBAY-REAL-H1")
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "OUTBOUND_CONFIRMED_BY_SYNC")) == 1
        assert db.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id == cid, Message.sender_role == "SELLER")) == 1  # 不多写一条
    # 之后同一条再被同步一次：按已记下的 ID 认出是本系统发的
    assert _reply(cid, "EBAY-REAL-H1-DUP", at2 + timedelta(minutes=3), body="Something else") == [j2]


def test_uncertain_send_other_text_or_late_is_colleague(client):
    from test_replied_on_ebay_acceptance import _reply, _state

    from app.db import session_scope
    from app.models import OutboundMessage

    cid, j1, j2, at2 = _uncertain_case(client, "buyer_h2")
    assert _reply(cid, "EBAY-COLLEAGUE-H2", at2 + timedelta(minutes=2), body="We ship tomorrow.") == [j2]
    with session_scope() as db:
        assert db.scalar(select(OutboundMessage.state).where(OutboundMessage.conversation_id == cid)) == "UNCERTAIN"

    cid, j1, j2, at2 = _uncertain_case(client, "buyer_h3")
    late = at2 + timedelta(days=2)  # 正文一样但隔了两天：不是那次发送
    assert _reply(cid, "EBAY-LATE-H3", late, body="Thanks, it is in stock. Best regards") == [j2]
