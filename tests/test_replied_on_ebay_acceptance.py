"""验收（测试工程师，2026-11-09 第三轮）：同事在 eBay 后台回复后自动关掉过时任务。
重点是误关：本系统自己发出的各种情况、时间相等 / 乱序；买家连发、中间夹着后台回复；生成中被回复不留副作用；
和“最后一句是买家”补建任务不打架。"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from test_conversation_state import BUYER, ITEM, delivered  # noqa: F401  delivered 是夹具
from test_pipeline import draft_json, send_buyer, use_fake_llm


def _setup(client, text="Is it in stock?", buyer="buyer_rep_1", state="REVIEW_REQUIRED"):
    """买家来一条 → 任务改成待审核（带一份活动草稿）。返回 (会话 ID, 任务 ID, 买家消息时间)。"""
    from app.db import session_scope
    from app.models import Draft, Message, ReplyJob

    cid = send_buyer(client, text, buyer=buyer)["conversation_id"]
    with session_scope() as db:
        job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid).order_by(ReplyJob.id.desc()))
        job.state = state
        db.add(Draft(job_id=job.id, run_generation=1, conversation_row_version=1, text="Yes.", language="en", intents=["stock"]))
        at = db.scalar(select(Message.sent_at).where(Message.conversation_id == cid,
                                                     Message.ebay_message_id == job.anchor_source_message_id))
        return cid, job.id, at


def _reply(cid, mid, at, body="Reply from eBay back office"):
    """模拟同步拉到一条店铺消息并触发关任务。返回关掉的任务 ID。"""
    from app import ebay_replied
    from app.db import session_scope
    from app.ingestion.service import _append_message
    from app.models import Conversation

    with session_scope() as db:
        conv = db.get(Conversation, cid)
        msg = _append_message(db, conv, mid, "SELLER", "OUT", body, at)
        return ebay_replied.close_replied_jobs(db, conv, msg, actor="ebay_sync")


def _state(job_id):
    from app.db import session_scope
    from app.models import ReplyJob

    with session_scope() as db:
        j = db.get(ReplyJob, job_id)
        return j.state, list(j.decision_reasons or [])


def _outbox(cid, state, pmid=None, body="Thanks, it is in stock.", job_id=None):
    from app.db import session_scope, utcnow
    from app.models import OutboundMessage

    with session_scope() as db:
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=cid, body=body, source="manual", actor="tester",
                               state=state, platform_message_id=pmid, send_after=utcnow(), job_id=job_id))


# ---------------------------------------------------------------- 1. 本系统发出的不能触发
def test_own_send_intent_id_not_trigger(client):
    from app.db import session_scope, utcnow
    from app.models import Draft, SendIntent

    cid, jid, at = _setup(client)
    with session_scope() as db:
        d = db.scalar(select(Draft).where(Draft.job_id == jid))
        db.add(SendIntent(job_id=jid, draft_id=d.id, text="Yes.", content_hash="x" * 64, idempotency_key="acc-1",
                          state="SENT", platform_message_id="EBAY-OWN-INTENT", sent_at=utcnow(), actor="tester"))
    assert _reply(cid, "EBAY-OWN-INTENT", at + timedelta(minutes=5)) == []
    assert _state(jid)[0] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("ob_state, pmid, closes", [
    ("SENT", "EBAY-OWN-OB", False),     # 发件箱已发出、记下了 eBay 消息 ID：同步回来的就是它
    ("SENDING", None, False),           # 正在发（已调 eBay、还没记下 ID）：先不算
    ("PENDING", None, True),            # 撤回期内还没发：同事后台回复照样关任务，待发的不动
    ("RECALLED", None, True),           # 撤回了：同事后台回复照样关
], ids=["sent_with_id", "sending", "pending_5min", "recalled"])
def test_outbox_states(client, ob_state, pmid, closes):
    from app.db import session_scope
    from app.models import OutboundMessage

    cid, jid, at = _setup(client, buyer=f"buyer_ob_{ob_state.lower()}")
    _outbox(cid, ob_state, pmid)
    mid = pmid or "EBAY-COLLEAGUE-1"
    closed = _reply(cid, mid, at + timedelta(minutes=5))
    assert (closed == [jid]) is closes
    assert _state(jid)[0] == ("RESOLVED_NO_REPLY" if closes else "REVIEW_REQUIRED")
    with session_scope() as db:  # 发件箱不动
        assert db.scalar(select(OutboundMessage.state).where(OutboundMessage.conversation_id == cid)) == ob_state


def test_uncertain_send_synced_back_not_treated_as_colleague(client):
    from app.db import session_scope
    from app.models import ReplyJob

    cid, j1, at = _setup(client, buyer="buyer_uncertain")
    with session_scope() as db:
        db.get(ReplyJob, j1).state = "SEND_UNCERTAIN"
    _outbox(cid, "UNCERTAIN", None, body="Thanks, it is in stock.", job_id=j1)
    _, j2, at2 = _setup(client, text="Also, can you ship today?", buyer="buyer_uncertain")  # 5 分钟延迟期内买家又来信
    assert j2 != j1
    closed = _reply(cid, "EBAY-REAL-ID-OF-OWN", at2 + timedelta(minutes=3), body="Thanks, it is in stock.")
    assert closed == [] and _state(j2)[0] == "REVIEW_REQUIRED"


# ---------------------------------------------------------------- 1. 时间边界
@pytest.mark.parametrize("delta, closes", [(timedelta(0), False), (timedelta(seconds=1), True),
                                           (timedelta(seconds=-1), False), (timedelta(days=-2), False)],
                         ids=["equal", "plus_1s", "minus_1s", "much_earlier"])
def test_time_boundaries(client, delta, closes):
    cid, jid, at = _setup(client, buyer=f"buyer_t_{int(delta.total_seconds())}".replace("-", "m"))
    assert (_reply(cid, f"EBAY-T-{delta.total_seconds()}", at + delta) == [jid]) is closes


def test_closable_states_only(client):
    """只关 RECEIVED / RETRYABLE_ERROR / REVIEW_REQUIRED / AUTO_ELIGIBLE / FAILED_HANDOFF；发送中、已发、发送不确定不动。"""
    for st, closes in (("RECEIVED", True), ("RETRYABLE_ERROR", True), ("AUTO_ELIGIBLE", True), ("FAILED_HANDOFF", True),
                       ("SENDING", False), ("SENT", False), ("SEND_UNCERTAIN", False), ("RUNNING", False)):
        cid, jid, at = _setup(client, buyer=f"buyer_st_{st.lower()}", state=st)
        assert (_reply(cid, f"EBAY-ST-{st}", at + timedelta(minutes=1)) == [jid]) is closes, st
        if closes:
            from app.db import session_scope
            from app.models import AuditEvent, ConversationEvent, Draft

            with session_scope() as db:
                assert db.scalar(select(Draft.status).where(Draft.job_id == jid)) == "SUPERSEDED"
                assert db.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == "JOB_CLOSED_REPLIED_ON_EBAY", AuditEvent.conversation_id == cid)) == 1
                assert db.scalar(select(func.count()).select_from(ConversationEvent).where(
                    ConversationEvent.conversation_id == cid, ConversationEvent.event_type == "REPLIED_ON_EBAY")) == 1


# ---------------------------------------------------------------- 2. 买家连发、中间夹着后台回复
def test_only_the_replied_round_is_closed(client):
    from app.db import session_scope
    from app.models import Message, ReplyJob, ReplyJobSource

    cid, j1, at1 = _setup(client, text="Is it in stock?", buyer="buyer_multi")
    assert _reply(cid, "EBAY-MID", at1 + timedelta(minutes=1)) == [j1]
    sent = send_buyer(client, "Great, can you ship today?", buyer="buyer_multi")
    with session_scope() as db:
        j2 = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid).order_by(ReplyJob.id.desc()))
        srcs = list(db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == j2.id)))
        last_buyer = db.scalar(select(Message.ebay_message_id).where(Message.conversation_id == cid, Message.sender_role == "BUYER")
                               .order_by(Message.conversation_seq.desc()))
    assert j2.id != j1 and j2.state == "RECEIVED" and srcs == [last_buyer]  # 新任务只覆盖回复之后的来信
    assert _state(j1)[0] == "RESOLVED_NO_REPLY"


def test_merged_job_replied_in_between_kept(client):
    """两条来信已合并成一个任务（覆盖到后一条），同事回复时间夹在两条之间：任务不关（后一条还没人回）。"""
    from app.db import session_scope
    from app.models import ReplyJob, ReplyJobSource

    cid, j1, at1 = _setup(client, text="Is it in stock?", buyer="buyer_merge")
    send_buyer(client, "And can you ship today?", buyer="buyer_merge")
    with session_scope() as db:
        job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid).order_by(ReplyJob.id.desc()))
        job.state = "REVIEW_REQUIRED"
        n_src = db.scalar(select(func.count()).select_from(ReplyJobSource).where(ReplyJobSource.job_id == job.id))
        jid = job.id
    from app import ebay_replied

    with session_scope() as db:
        anchor = ebay_replied.anchor_time(db, db.get(ReplyJob, jid))
    assert anchor > at1
    assert _reply(cid, "EBAY-BETWEEN", at1 + (anchor - at1) / 2) == []
    assert _state(jid)[0] == "REVIEW_REQUIRED" and n_src >= 1


# ---------------------------------------------------------------- 3. 生成中被回复：不进待审核、没有副作用
@pytest.mark.parametrize("replied", [False, True], ids=["control_no_reply", "replied_during_run"])
def test_running_replied_has_no_side_effects(client, delivered, monkeypatch, replied):
    from app import config
    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message
    from app.models import (AftersalesCase, Conversation, Draft, LogisticsTicket, OutboundMessage, ParcelFollowup,
                            ReplyJob)
    from app.worker import worker

    monkeypatch.setenv("LOGISTICS_TICKET_AUTO_DRAFT", "true")
    config.get_settings.cache_clear()
    use_fake_llm(draft_json(intent="delivery_delay", intents=["delivery_delay"], action="handoff", needs_human=True, claims=[]))
    cid = send_buyer(client, "Tracking says delivered but I have not received my parcel.", buyer=BUYER, item=ITEM)["conversation_id"]
    if replied:
        with session_scope() as db:
            _append_message(db, db.get(Conversation, cid), "EBAY-DURING-RUN", "SELLER", "OUT", "We are checking.",
                            utcnow() + timedelta(seconds=5))
    worker.run_once()
    with session_scope() as db:
        job = db.scalar(select(ReplyJob).where(ReplyJob.conversation_id == cid))
        counts = {m.__name__: db.scalar(select(func.count()).select_from(m).where(m.conversation_id == cid))
                  for m in (LogisticsTicket, ParcelFollowup, AftersalesCase, OutboundMessage)}
        drafts = db.scalar(select(func.count()).select_from(Draft).where(Draft.job_id == job.id))
        conv = db.get(Conversation, cid)
        situation = conv.situation_label
    if not replied:  # 对照：没被回复时这条会登记异常包裹 / 起草查件单，证明用例能测出副作用
        assert job.state == "REVIEW_REQUIRED" and drafts == 1
        assert counts["ParcelFollowup"] + counts["LogisticsTicket"] >= 1
    else:
        assert job.state == "RESOLVED_NO_REPLY" and "REPLIED_ON_EBAY" in job.decision_reasons and drafts == 0
        assert counts == {"LogisticsTicket": 0, "ParcelFollowup": 0, "AftersalesCase": 0, "OutboundMessage": 0}
        assert situation is None


# ---------------------------------------------------------------- 4. 和“最后一句是买家”补建任务不打架
def test_no_reopen_after_close(client):
    from app.db import session_scope
    from app.ingestion.service import open_job_for_unanswered
    from app.models import Conversation, ReplyJob

    cid, jid, at = _setup(client, buyer="buyer_reopen")
    assert _reply(cid, "EBAY-R1", at + timedelta(minutes=1)) == [jid]
    with session_scope() as db:
        assert open_job_for_unanswered(db, db.get(Conversation, cid), actor="ebay_sync") is None
        assert db.scalar(select(func.count()).select_from(ReplyJob).where(ReplyJob.conversation_id == cid)) == 1


def test_no_reopen_when_seq_out_of_time_order(client):
    """同步顺序和时间不一致：店铺回复先入库（序号小）、时间却晚于买家来信 → 任务按时间关掉；补建任务按序号看最后是买家，
    但那条买家消息已处理过，不会再补建。"""
    from app.db import session_scope
    from app.ingestion.service import open_job_for_unanswered
    from app.models import Conversation, Message, ReplyJob

    cid, jid, at = _setup(client, buyer="buyer_seq")
    closed = _reply(cid, "EBAY-SEQ", at + timedelta(minutes=1))
    with session_scope() as db:  # 把店铺回复的序号挪到买家来信前面
        msgs = db.scalars(select(Message).where(Message.conversation_id == cid).order_by(Message.conversation_seq)).all()
        b, s = next(m for m in msgs if m.sender_role == "BUYER"), next(m for m in msgs if m.ebay_message_id == "EBAY-SEQ")
        b.conversation_seq, s.conversation_seq = s.conversation_seq + 100, b.conversation_seq
    assert closed == [jid]
    with session_scope() as db:
        assert open_job_for_unanswered(db, db.get(Conversation, cid), actor="ebay_sync") is None
        assert db.scalar(select(func.count()).select_from(ReplyJob).where(ReplyJob.conversation_id == cid)) == 1


# ---------------------------------------------------------------- 5. 存量脚本默认只预览
def test_backfill_script_preview_does_not_change(client, capsys):
    import importlib.util
    from pathlib import Path

    cid, jid, at = _setup(client, buyer="buyer_backfill")
    from app.db import session_scope
    from app.ingestion.service import _append_message
    from app.models import Conversation

    with session_scope() as db:
        _append_message(db, db.get(Conversation, cid), "EBAY-OLD-REPLY", "SELLER", "OUT", "Answered in back office",
                        at + timedelta(minutes=2))
    spec = importlib.util.spec_from_file_location("close_replied", Path(__file__).resolve().parents[1] / "scripts" / "close_replied_on_ebay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "共 1 个任务" in out and "这是预览" in out and _state(jid)[0] == "REVIEW_REQUIRED"
    assert mod.main(["--apply"]) == 0 and _state(jid) == ("RESOLVED_NO_REPLY", _state(jid)[1])
    assert "REPLIED_ON_EBAY" in _state(jid)[1]


# ---------------------------------------------------------------- H 修复补测（2026-11-10）：正文空白不同 / 真不同 / 时间窗
OWN_BODY = "Thanks, it is in stock.\nWe ship today."


def _uncertain_case(client, buyer, ob_state="UNCERTAIN", body=OWN_BODY):
    from app.db import session_scope
    from app.models import ReplyJob

    cid, j1, at = _setup(client, buyer=buyer)
    with session_scope() as db:
        db.get(ReplyJob, j1).state = "SEND_UNCERTAIN"
    _outbox(cid, ob_state, None, body=body, job_id=j1)
    _, j2, at2 = _setup(client, text="Also, can you ship today?", buyer=buyer)
    return cid, j1, j2, at2


def _ob(cid):
    from app.db import session_scope
    from app.models import OutboundMessage

    with session_scope() as db:
        ob = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_id == cid))
        return ob.state, ob.platform_message_id, ob.message_id


@pytest.mark.parametrize("synced", [
    "Thanks,  it is in stock.\r\nWe ship today. ",
    "\tThanks, it is in stock. We ship today.",
    "Thanks,\u00a0it is in stock.\n\n\nWe ship today.",
], ids=["crlf_double_space", "tab_no_newline", "nbsp_blank_lines"])
def test_uncertain_whitespace_variants_recognized(client, synced):
    from app.db import session_scope
    from app.models import AuditEvent, Message, ReplyJob

    cid, j1, j2, at2 = _uncertain_case(client, f"buyer_ws_{abs(hash(synced)) % 1000}")
    with session_scope() as db:
        n_before = db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid))
    assert _reply(cid, "EBAY-SYNCED-OWN", at2 + timedelta(minutes=3), body=synced) == []
    assert _state(j2)[0] == "REVIEW_REQUIRED"  # 发件期间买家的新来信：不关
    state, pmid, mid = _ob(cid)
    assert state == "SENT" and pmid == "EBAY-SYNCED-OWN" and mid is not None
    with session_scope() as db:
        assert db.get(ReplyJob, j1).state == "SENT"  # 发送不确定的任务补成已发出
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "OUTBOUND_CONFIRMED_BY_SYNC", AuditEvent.conversation_id == cid)) == 1
        # 只有同步写进来的那一条，没有多写
        assert db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid)) == n_before + 1


@pytest.mark.parametrize("body, delta, closes", [
    ("Sorry, we are out of stock.", timedelta(minutes=3), True),      # 正文真不一样：同事回复
    (OWN_BODY, timedelta(hours=25), True),                           # 正文一样但隔了 25 小时：超出时间窗
    (OWN_BODY, timedelta(minutes=3), False),                         # 对照
], ids=["different_text", "too_late", "control_same"])
def test_uncertain_different_or_late_still_closes(client, body, delta, closes):
    cid, j1, j2, at2 = _uncertain_case(client, f"buyer_df_{int(delta.total_seconds())}_{len(body)}")
    closed = _reply(cid, "EBAY-OTHER", at2 + delta, body=body)
    assert (closed == [j2]) is closes
    assert _ob(cid)[0] == ("UNCERTAIN" if closes else "SENT")


def test_sent_without_platform_id_recognized(client):
    cid, j1, j2, at2 = _uncertain_case(client, "buyer_sent_noid", ob_state="SENT")
    assert _reply(cid, "EBAY-NOID", at2 + timedelta(minutes=3), body=OWN_BODY) == []
    assert _ob(cid)[1] == "EBAY-NOID"


def test_uncertain_only_first_2000_chars_compared(client):
    """记录现状：只比前 2000 字（eBay 只发前 2000 字），之后不同也认成本系统发出的。"""
    long = "x" * 2000
    cid, j1, j2, at2 = _uncertain_case(client, "buyer_long", body=long + " tail A")
    assert _reply(cid, "EBAY-LONG", at2 + timedelta(minutes=3), body=long + " tail B") == []
