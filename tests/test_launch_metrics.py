"""上线效果统计：来信轮次与首次回复时长（“已收到”回执不算回复、系统通知不算来信、超出时间范围不算）、
AI 草稿修改分档与采纳率、没起草的原因、转人工原因（闸门说明不计）、客服效率、售后与物流、大模型用量，以及店铺权限。"""
from datetime import timedelta

from fastapi.testclient import TestClient

from conftest import login, make_user

H = 3600


def seed():
    from app.db import session_scope, utcnow
    from app.models import (AftersalesCase, Conversation, Draft, Invoice, LlmUsage, LogisticsTicket, Message,
                            OutboundMessage, ParcelFollowup, ReplyJob, ReviewFeedback)

    now = utcnow()
    ago = lambda hours: now - timedelta(hours=hours)  # noqa: E731
    with session_scope() as db:
        def conv(store, key, kind="FROM_MEMBERS"):
            c = Conversation(store_id=store, ebay_conversation_id=key, buyer_key=f"b-{key}", conversation_type=kind)
            db.add(c)
            db.flush()
            return c

        def msg(c, seq, role, direction, at, kind=None):
            db.add(Message(store_id=c.store_id, ebay_message_id=f"{c.ebay_conversation_id}-{seq}", conversation_id=c.id,
                           conversation_seq=seq, sender_role=role, direction=direction, sent_at=at, body="x", auto_kind=kind))

        c1, c2, c3, sys_c = conv("STORE_A", "c1"), conv("STORE_A", "c2"), conv("STORE_B", "c3"), conv("STORE_A", "s1", "FROM_EBAY")
        # c1：两条连着的来信算一轮，“已收到”回执不算回复 → 2 小时；第二轮 1 小时；最后一条买家来信还没回
        msg(c1, 1, "BUYER", "IN", ago(50))
        msg(c1, 2, "BUYER", "IN", ago(50) + timedelta(minutes=10))
        msg(c1, 3, "SELLER", "OUT", ago(50) + timedelta(minutes=20), kind="ACK")
        msg(c1, 4, "SELLER", "OUT", ago(48))
        msg(c1, 5, "BUYER", "IN", ago(30))
        msg(c1, 6, "SELLER", "OUT", ago(29))
        msg(c1, 7, "BUYER", "IN", ago(3))
        # c2：30 小时后才回（超过 24 小时）
        msg(c2, 1, "BUYER", "IN", ago(40))
        msg(c2, 2, "SELLER", "OUT", ago(10))
        # c3（STORE_B）：20 天前的一轮不在范围里；最近一条没回
        msg(c3, 1, "BUYER", "IN", ago(20 * 24))
        msg(c3, 2, "SELLER", "OUT", ago(19 * 24))
        msg(c3, 3, "BUYER", "IN", ago(5))
        msg(sys_c, 1, "SYSTEM", "IN", ago(5))  # 系统通知不算

        n = [0]

        def job(c, state="REVIEW_REQUIRED", reasons=None, intent=None):
            n[0] += 1
            j = ReplyJob(store_id=c.store_id, conversation_id=c.id, anchor_source_message_id=f"m{n[0]}", state=state,
                         decision_reasons=reasons, created_at=ago(20))
            db.add(j)
            db.flush()
            if intent:
                db.add(Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1,
                             text="draft", intent=intent))
            return j

        j1 = job(c1, reasons=["AUTO_SEND_DISABLED", "HAS_ATTACHMENTS"], intent="fitment")
        j2 = job(c1, reasons=["BUYER_BLACKLISTED"])
        job(c3, state="FAILED_HANDOFF")
        job(c1, state="CANCELLED_STALE", intent="fitment")  # 被合并作废的不算
        j5 = job(c2, intent="shipping_status")
        j6 = job(c2, intent="shipping_status")
        j7 = job(c3, intent="return_refund")
        db.flush()

        def fb(j, who, action, ratio=None, ms=None):
            db.add(ReviewFeedback(job_id=j.id, reviewer=who, action=action, edit_ratio=ratio, handling_ms=ms, created_at=ago(19)))

        fb(j1, "小王", "APPROVE", 0.0, 60_000)
        fb(j5, "小王", "EDIT_APPROVE", 0.05, 120_000)
        fb(j5, "小王", "REJECT")
        fb(j6, "小李", "EDIT_APPROVE", 0.2)
        fb(j7, "小李", "EDIT_APPROVE", 0.6)
        fb(j2, "小李", "MANUAL")

        def out(c, actor, source, state, at):
            db.add(OutboundMessage(store_id=c.store_id, conversation_id=c.id, body="x", source=source, actor=actor, state=state,
                                   send_after=at, created_at=at, sent_at=at if state == "SENT" else None))

        out(c1, "小王", "manual", "SENT", ago(29))
        out(c1, "小王", "manual", "RECALLED", ago(28))
        out(c3, "小李", "manual", "FAILED", ago(4))
        out(c2, "系统", "auto", "SENT", ago(10))
        out(c1, "系统", "ack", "SENT", ago(49))

        cn = lambda hours: (ago(hours) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731  谷仓是北京时间
        db.add(LogisticsTicket(io_code="CJ1", store_id="STORE_A", added_at=cn(48), replied_at=cn(42), origin="AI_DRAFT", send_status="SENT"))
        db.add(LogisticsTicket(io_code="CJ2", store_id="STORE_A", added_at=cn(24)))
        db.add(LogisticsTicket(io_code="DRAFT-1", store_id="STORE_A", origin="AI_DRAFT", created_at=ago(2)))  # 草稿没提交
        db.add(LogisticsTicket(io_code="CJ3", store_id="STORE_B", added_at=cn(12), replied_at=cn(10)))

        sent = ago(20)
        db.add(AftersalesCase(conversation_id=c1.id, store_id="STORE_A", case_type="售后", category="质量问题", problem_zh="坏了",
                              original_text="broken", status="ANSWERED", sent_at=sent, answered_at=sent + timedelta(hours=5),
                              replies=[{"text": "补发", "at": (sent + timedelta(hours=3)).isoformat() + "Z"}]))

        db.add(ParcelFollowup(owner_id=1, owner_name="小王", store_id="STORE_A", reason="断更", status="DONE",
                              created_at=ago(4 * 24), closed_at=ago(24)))
        db.add(ParcelFollowup(owner_id=1, owner_name="小王", store_id="STORE_A", reason="无轨迹", status="OPEN", origin="AUTO",
                              created_at=ago(24)))
        db.add(ParcelFollowup(owner_id=1, owner_name="小李", store_id="STORE_B", reason="断更", status="OPEN", created_at=ago(24)))

        def inv(no, store, by, status):
            db.add(Invoice(number=no, store_id=store, order_id="01", issuer_id=1, issue_date="2026-11-09", language="de",
                           currency="EUR", total=10, snapshot={}, file_path="x.pdf", sha256="0", status=status, created_by=by,
                           created_at=ago(6)))

        inv("I-1", "STORE_A", "agent", "SENT")
        inv("I-2", "STORE_B", "小李", "ISSUED")

        db.add(LlmUsage(purpose="reply", store_id="STORE_A", input_tokens=100, output_tokens=20, total_tokens=120, ok=True, created_at=ago(6)))
        db.add(LlmUsage(purpose="translate", store_id="STORE_A", input_tokens=10, output_tokens=5, ok=False, created_at=ago(6)))
        db.add(LlmUsage(purpose="embedding_docs", store_id=None, total_tokens=50, estimated=True, ok=True, created_at=ago(6)))
        db.add(LlmUsage(purpose="reply", store_id="STORE_A", total_tokens=999, ok=True, created_at=ago(30 * 24)))  # 范围外


def by_key(rows, key="key"):
    return {r[key]: r for r in rows}


def test_launch_metrics_admin(client):
    seed()
    r = client.get("/api/performance/launch", params={"days": 7})
    assert r.status_code == 200, r.text
    d = r.json()
    m = d["messages"]
    assert (m["buyer_messages"], m["conversations"], m["replied_conversations"]) == (6, 3, 2)
    assert (m["reply_rounds"], m["replied_rounds"]) == (5, 3)
    assert m["first_response_median"] == 2 * H and m["first_response_p90"] == 30 * H  # 2h、1h、30h
    assert m["within_24h_rate"] == 0.4  # 2 轮 24 小时内 ÷ 5 轮
    assert (m["unreplied_now"], m["unreplied_over_24h"]) == (2, 0)
    assert m["sent_via_system_total"] == 3 and by_key(m["sent_via_system"])["ack"]["label"] == "已收到回执"
    assert sum(x["buyer"] for x in m["days"]) == 6 and sum(x["replies"] for x in m["days"]) == 3

    a = d["ai"]
    assert (a["jobs"], a["drafted"], a["no_draft_total"]) == (6, 4, 2)
    nd = by_key(a["no_draft"])
    assert nd["BUYER_BLACKLISTED"]["count"] == 1 and nd["RUN_FAILED"]["count"] == 1
    review = by_key(a["review"])
    assert [review[k]["count"] for k in ("adopted", "tweaked", "edited", "rewritten", "manual", "rejected")] == [1, 1, 1, 1, 1, 1]
    assert a["review_sent_total"] == 5 and a["adoption_rate"] == 0.4 and a["avg_edit_ratio"] == 0.2125
    assert review["rejected"]["share"] is None
    handoff = by_key(a["handoff_reasons"])
    assert "AUTO_SEND_DISABLED" not in handoff and handoff["HAS_ATTACHMENTS"]["count"] == 1
    assert by_key(a["intents"])["shipping_status"]["count"] == 2 and by_key(a["intents"])["fitment"]["label"]

    staff = by_key(d["staff"], "actor")
    w, li = staff["小王"], staff["小李"]
    assert (w["reviewed"], w["approved"], w["edited"], w["rejected"], w["median_handling_seconds"]) == (3, 1, 1, 1, 90)
    assert (w["submitted"], w["recalled"]) == (2, 1) and (li["manual"], li["failed"]) == (1, 1)
    assert "系统" not in staff  # AI 自动回复、回执不算客服

    s = d["aftersales"]
    assert (s["tickets"]["count"], s["tickets"]["replied"], s["tickets"]["results_sent"]) == (3, 2, 1)
    assert s["tickets"]["avg_reply_seconds"] == 4 * H  # (6h + 2h) / 2
    assert (s["feishu"]["count"], s["feishu"]["avg_group_reply_seconds"], s["feishu"]["avg_answer_seconds"]) == (1, 3 * H, 5 * H)
    p = s["parcels"]
    assert (p["new"], p["auto_new"], p["done"], p["open_now"], p["avg_days_to_close"]) == (3, 1, 1, 2, 3.0)
    assert (s["invoices"]["count"], s["invoices"]["auto"], s["invoices"]["sent"]) == (2, 1, 1)

    llm = d["llm"]
    assert llm["total"]["calls"] == 3 and llm["total"]["failed"] == 1 and llm["total"]["total_tokens"] == 120 + 15 + 50
    assert by_key(llm["by_purpose"], "purpose")["embedding_docs"]["estimated"] == 1
    assert set(d["notes"]) >= {"first_response", "adoption_rate", "no_draft", "llm"}


def test_launch_metrics_store_scope(client):
    seed()
    make_user("cs_a", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_a")
    d = cs.get("/api/performance/launch").json()
    m = d["messages"]
    assert (m["buyer_messages"], m["conversations"], m["unreplied_now"]) == (5, 2, 1)
    assert d["ai"]["jobs"] == 4 and {x["actor"] for x in d["staff"]} == {"小王", "小李"}
    assert by_key(d["staff"], "actor")["小李"]["failed"] == 0  # STORE_B 的发送失败看不到
    assert d["aftersales"]["tickets"]["count"] == 2 and d["aftersales"]["parcels"]["open_now"] == 1
    assert d["aftersales"]["invoices"]["count"] == 1
    assert d["llm"]["total"]["calls"] == 2  # 没有店铺的调用（知识向量化）只有看全部店铺时才算
    assert cs.get("/api/performance/launch", params={"store_id": "STORE_B"}).status_code in (403, 404)
    one = client.get("/api/performance/launch", params={"store_id": "STORE_B"}).json()
    assert one["messages"]["buyer_messages"] == 1 and one["ai"]["jobs"] == 2


def test_launch_metrics_range_validation(client):
    assert client.get("/api/performance/launch", params={"start": "2026-11-09", "end": "2026-11-01"}).status_code == 422
    assert client.get("/api/performance/launch", params={"start": "2024-01-01", "end": "2026-11-01"}).status_code == 422
    empty = client.get("/api/performance/launch", params={"start": "2020-01-01", "end": "2020-01-02"}).json()
    assert empty["messages"]["first_response_median"] is None and empty["ai"]["adoption_rate"] is None
    assert len(empty["messages"]["days"]) == 2
