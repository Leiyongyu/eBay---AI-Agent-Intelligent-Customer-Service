"""上线效果统计 · 验收用例（测试工程师）：补开发用例没覆盖的边界——只有 ACK- 前缀的旧回执、本机日期跨零点、
连续多条来信算一轮、范围外的来信在范围内回复、修改比例分档边界、P90 取值、合并作废的任务、eBay 系统会话、
没有店铺的客服、管理员按店铺筛选、客服查别的店铺。"""
from datetime import date, datetime, timedelta, timezone

from conftest import login, make_user

D0 = date(2026, 11, 3)  # 统计范围 11-03 ~ 11-05（本机日期）
D1 = date(2026, 11, 5)
H = 3600


def local(y, m, d, hh, mm=0) -> datetime:
    """本机时间 → 库里存的 UTC（无时区）。"""
    return datetime(y, m, d, hh, mm).astimezone().astimezone(timezone.utc).replace(tzinfo=None)


def seed(legacy_ack: bool = False):
    from app.db import session_scope
    from app.models import Conversation, Draft, Message, OutboundMessage, ReplyJob, ReviewFeedback

    with session_scope() as db:
        def conv(store, key, kind="FROM_MEMBERS"):
            c = Conversation(store_id=store, ebay_conversation_id=key, buyer_key=f"b-{key}", conversation_type=kind)
            db.add(c)
            db.flush()
            return c

        def msg(c, seq, role, direction, at, kind=None, mid=None):
            db.add(Message(store_id=c.store_id, ebay_message_id=mid or f"{c.ebay_conversation_id}-{seq}", conversation_id=c.id,
                           conversation_seq=seq, sender_role=role, direction=direction, sent_at=at, body="x", auto_kind=kind))

        # a1：本机 11-03 00:30 来信（UTC 是前一天）→ 算 11-03；15 分钟后发了“已收到”回执（不算回复），2 小时后正式回复。
        # legacy_ack=True 时回执是旧格式：只有 SIM-OUT-ACK- 前缀、没标 auto_kind（工作台按这两种都认）
        a1 = conv("STORE_A", "a1")
        msg(a1, 1, "BUYER", "IN", local(2026, 11, 3, 0, 30))
        msg(a1, 2, "SELLER", "OUT", local(2026, 11, 3, 0, 45), mid="SIM-OUT-ACK-legacy01",
            kind=None if legacy_ack else "ACK")
        msg(a1, 3, "SELLER", "OUT", local(2026, 11, 3, 2, 30))
        # a2：连续 3 条来信算一轮（从第一条算）→ 4 小时回复
        a2 = conv("STORE_A", "a2")
        for i, hh in enumerate((9, 10, 11), 1):
            msg(a2, i, "BUYER", "IN", local(2026, 11, 4, hh))
        msg(a2, 4, "SELLER", "OUT", local(2026, 11, 4, 13))
        # a3：来信在范围前（11-02 23:00 本机），范围内回复 → 这一轮不算
        a3 = conv("STORE_A", "a3")
        msg(a3, 1, "BUYER", "IN", local(2026, 11, 2, 23))
        msg(a3, 2, "SELLER", "OUT", local(2026, 11, 3, 1))
        # a4：本机 11-05 23:59 来信，没回复 → 算一轮、不算已回复；11-06 00:01 的来信不在范围
        a4 = conv("STORE_A", "a4")
        msg(a4, 1, "BUYER", "IN", local(2026, 11, 5, 23, 59))
        msg(a4, 2, "BUYER", "IN", local(2026, 11, 6, 0, 1))
        # s1：eBay 系统会话里的买家消息也不算
        s1 = conv("STORE_A", "s1", "FROM_EBAY")
        msg(s1, 1, "BUYER", "IN", local(2026, 11, 4, 9))
        # b1（STORE_B）：1 轮，1 小时回复
        b1 = conv("STORE_B", "b1")
        msg(b1, 1, "BUYER", "IN", local(2026, 11, 4, 8))
        msg(b1, 2, "SELLER", "OUT", local(2026, 11, 4, 9))

        # 回复任务：1 个被合并作废（不算）；审核反馈卡分档边界：0、0.0999、0.1、0.4
        jobs = []
        for i, (c, state) in enumerate(((a1, "SENT"), (a2, "SENT"), (a2, "CANCELLED_STALE"), (a4, "REVIEW_REQUIRED"),
                                        (b1, "SENT")), 1):
            j = ReplyJob(store_id=c.store_id, conversation_id=c.id, anchor_source_message_id=f"j{i}", state=state,
                         created_at=local(2026, 11, 4, 12))
            db.add(j)
            db.flush()
            db.add(Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="d",
                         intent="shipping_status"))
            jobs.append(j)
        db.flush()
        for j, action, ratio in ((jobs[0], "EDIT_APPROVE", 0.0999), (jobs[1], "EDIT_APPROVE", 0.1),
                                 (jobs[3], "EDIT_APPROVE", 0.4), (jobs[4], "APPROVE", None)):
            db.add(ReviewFeedback(job_id=j.id, draft_id=None, reviewer="小王", action=action, edit_ratio=ratio,
                                  created_at=local(2026, 11, 4, 14), reason_codes=[]))
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=a1.id, body="x", actor="小王", source="manual",
                               state="SENT", created_at=local(2026, 11, 3, 2), send_after=local(2026, 11, 3, 2), sent_at=local(2026, 11, 3, 2, 30)))
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=a1.id, body="x", actor="AI（已收到回执）", source="ack",
                               state="SENT", created_at=local(2026, 11, 3, 0, 40), send_after=local(2026, 11, 3, 0, 40), sent_at=local(2026, 11, 3, 0, 45)))


def get(c, **params):
    r = c.get("/api/performance/launch", params={"start": D0.isoformat(), "end": D1.isoformat(), **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_edges_admin_all_stores(client):
    seed()
    d = get(client)
    m = d["messages"]
    # 来信：a1 1 + a2 3 + a4 1（11-06 那条不算）+ b1 1 = 6；系统会话、范围前的 a3 不算
    assert m["buyer_messages"] == 6, m
    assert m["conversations"] == 4  # a1 a2 a4 b1
    # 轮次：a1、a2、a4（未回）、b1 = 4；a3 来信在范围前不算
    assert (m["reply_rounds"], m["replied_rounds"]) == (4, 3), m
    # 回复时长：a1 2h（回执不算）、a2 4h（从第一条算）、b1 1h → 中位数 2h、P90 4h
    assert m["first_response_median"] == 2 * H, m
    assert m["first_response_p90"] == 4 * H
    assert m["within_24h_rate"] == 0.75  # 3 ÷ 4（没回的算在分母）
    days = {x["date"]: x for x in m["days"]}
    assert days["2026-11-03"]["buyer"] == 1  # 本机 00:30 的来信算 11-03，不算 11-02
    a = d["ai"]
    assert a["jobs"] == 4  # 合并作废的不算
    rv = {x["key"]: x["count"] for x in a["review"]}
    # 分档边界：0.0999 微调、0.1 小改、0.4 大改、APPROVE 原样
    assert (rv["adopted"], rv["tweaked"], rv["edited"], rv["rewritten"]) == (1, 1, 1, 1), rv
    assert a["adoption_rate"] == 0.5
    assert {x["key"]: x["count"] for x in m["sent_via_system"]} == {"manual": 1, "ack": 1}


def test_store_filter_and_staff_scope(client):
    seed()
    one = get(client, store_id="STORE_B")  # 管理员按店铺筛选
    assert (one["messages"]["buyer_messages"], one["messages"]["replied_rounds"], one["ai"]["jobs"]) == (1, 1, 1)
    make_user("kefu_a", "AGENT", stores=("STORE_A",))
    make_user("kefu_none", "AGENT")
    from fastapi.testclient import TestClient

    from app.server import create_app

    ca = TestClient(create_app())
    login(ca, "kefu_a")
    mine = get(ca)
    assert mine["messages"]["buyer_messages"] == 5 and mine["ai"]["jobs"] == 3  # 只有 STORE_A
    # 查别的店铺要被拒绝。验收要求写的是 403；系统统一用 check_store 返回 404（不暴露店铺是否存在），两者都算拒绝
    assert ca.get("/api/performance/launch", params={"store_id": "STORE_B"}).status_code in (403, 404)
    cn = TestClient(create_app())
    login(cn, "kefu_none")
    none = get(cn)  # 没分配店铺：什么都看不到（不能变成看全部）
    assert none["messages"]["buyer_messages"] == 0 and none["ai"]["jobs"] == 0 and none["llm"]["total"]["calls"] == 0


def test_legacy_ack_prefix_not_counted_as_reply(client):
    seed(legacy_ack=True)
    assert get(client)["messages"]["first_response_median"] == 2 * H


def test_ranges(client):
    """时间范围：今天 / 7 天 / 30 天（days 参数）、自定义、非法范围。"""
    from datetime import date as _d

    today = _d.today()
    for days, n in ((1, 1), (7, 7), (30, 30)):
        r = client.get("/api/performance/launch", params={"days": days})
        assert r.status_code == 200 and len(r.json()["messages"]["days"]) == n, (days, r.text[:200])
        assert r.json()["end"] == today.isoformat()
    r = client.get("/api/performance/launch", params={"start": "2026-11-01", "end": "2026-11-01"})
    assert r.status_code == 200 and len(r.json()["messages"]["days"]) == 1  # 自定义一天
    assert client.get("/api/performance/launch", params={"start": "2026-11-05", "end": "2026-11-04"}).status_code == 422
    assert client.get("/api/performance/launch", params={"start": "2025-11-01", "end": "2026-11-02"}).status_code == 422
    assert client.get("/api/performance/launch", params={"start": "2025-11-03", "end": "2026-11-03"}).status_code == 200  # 正好 366 天
    assert client.get("/api/performance/launch", params={"start": "11/01/2026"}).status_code == 422  # 日期格式不对
