"""规则：会话最后一句永远是我们的。拉取到最后一条是买家消息、又没有待处理任务的会话（超过 24 小时的旧来信、首次同步）：
补一个回复任务进“未处理”；最后一条是我们发的、已有任务、这条已处理过的不建。"""


def test_open_job_for_unanswered(app_env):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation, open_job_for_unanswered
    from app.models import JobState, ReplyJob, ReplyJobSource
    from sqlalchemy import select

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-UNA", "buyer_una", None, "EBAY_DE", None)
        _append_message(db, conv, "M1", "BUYER", "IN", "Hallo?", None)
        _append_message(db, conv, "M2", "SELLER", "OUT", "Hallo!", None)
        assert open_job_for_unanswered(db, conv) is None  # 最后一句是我们的
        _append_message(db, conv, "M3", "BUYER", "IN", "Wo ist mein Paket?", None)
        _append_message(db, conv, "M4", "BUYER", "IN", "Bitte antworten", None)
        job = open_job_for_unanswered(db, conv)
        assert job.state == JobState.RECEIVED and job.anchor_source_message_id == "M4"
        assert sorted(db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == job.id))) == ["M3", "M4"]
        assert open_job_for_unanswered(db, conv) is None  # 已有任务：不重复
        job.state = JobState.RESOLVED_NO_REPLY
        db.flush()
        assert open_job_for_unanswered(db, conv) is None  # 这条已经处理过：不再建
        assert len(db.scalars(select(ReplyJob)).all()) == 1
