"""绩效统计：按客服的回复量、撤回、响应时长、超时、AI 草稿采纳，以及 AI 自动回复单独一行。"""
from datetime import timedelta

from test_conversation_marks import conv


def _reply(cid, actor, body, *, source="manual", state="SENT", agent=None, waited_h=1.0):
    from app.db import session_scope, utcnow
    from app.models import Draft, OutboundMessage, ReplyJob

    now = utcnow()
    with session_scope() as db:
        job = ReplyJob(store_id="STORE_A", conversation_id=cid, anchor_source_message_id=f"SIM-{cid}-{body[:6]}",
                       state="SENT", created_at=now - timedelta(hours=waited_h))
        db.add(job)
        db.flush()
        if agent is not None:
            db.add(Draft(job_id=job.id, run_generation=1, conversation_row_version=1, text=agent, author="agent"))
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=cid, body=body, source=source, actor=actor,
                               job_id=job.id, state=state, send_after=now, created_at=now))


def test_performance_summary_by_staff(client):
    a, b, c = (conv("STORE_A", k) for k in ("PF1", "PF2", "PF3"))
    _reply(a, "小雷", "Hello  there", agent="Hello there", waited_h=2)  # 直接采纳（只差空格）
    _reply(b, "小雷", "Hi, edited", agent="Hi", waited_h=30)  # 修改后发送、超时
    _reply(c, "小雷", "recalled one", state="RECALLED")
    _reply(c, "王东", "written by hand", waited_h=0.5)  # 没有 AI 草稿 = 人工撰写
    _reply(a, "AI自动回复", "auto answer", source="auto", agent="auto answer", waited_h=0.01)

    d = client.get("/api/performance/summary", params={"days": 1}).json()
    lei = next(p for p in d["people"] if p["actor"] == "小雷")
    assert (lei["submitted"], lei["sent"], lei["recalled"], lei["conversations"]) == (3, 2, 1, 2)
    assert (lei["adopted"], lei["edited"], lei["own"], lei["late"]) == (1, 1, 0, 1)
    assert lei["adoption_rate"] == 0.5 and lei["on_time_rate"] == 0.5
    assert 2 * 3600 - 5 <= lei["median_response_seconds"] <= 30 * 3600
    dong = next(p for p in d["people"] if p["actor"] == "王东")
    assert dong["own"] == 1 and dong["late"] == 0
    assert d["ai"]["submitted"] == 1 and d["ai"]["adopted"] == 1
    t = d["totals"]
    assert (t["manual"], t["auto"], t["replies"], t["staff"]) == (3, 1, 4, 2)
    assert sum(x["manual"] for x in d["days"]) == 3 and len(d["days"]) == 1

    assert client.get("/api/performance/summary", params={"start": "2026-02-02", "end": "2026-01-01"}).status_code == 422
