"""工作台会话列表性能：批量查询（SQL 次数和会话数无关）、增量刷新的变更标记、“有 AI 草稿”筛选（原来循环里变量被覆盖）、
列表查询用的索引启动时补建。"""
from datetime import timedelta

from sqlalchemy import event


def seed(n, prefix="P"):
    """n 个买家会话，每个都带消息、回复任务、进行中的草稿；部分带标记、发件箱、认领、发票申请、黑名单买家。"""
    from app.db import session_scope, utcnow
    from app.models import (BuyerBlacklist, Conversation, ConversationMark, Draft, InvoiceRequest, Message, MessageTag,
                            OutboundMessage, ReplyJob, WorkClaim)

    now = utcnow()
    with session_scope() as db:
        tag = db.query(MessageTag).first()
        for i in range(n):
            c = Conversation(store_id="STORE_A", ebay_conversation_id=f"{prefix}{i}", buyer_key=f"{prefix.lower()}buyer{i}",
                             conversation_type="FROM_MEMBERS", item_id="110000000001", marketplace_id="EBAY_US",
                             last_message_at=now - timedelta(minutes=i))
            db.add(c)
            db.flush()
            db.add(Message(store_id="STORE_A", ebay_message_id=f"{prefix}{i}-1", conversation_id=c.id, conversation_seq=1,
                           sender_role="BUYER", direction="IN", sent_at=now - timedelta(hours=2), body=f"hello {i}"))
            db.add(Message(store_id="STORE_A", ebay_message_id=f"SIM-OUT-{prefix}{i}", conversation_id=c.id, conversation_seq=2,
                           sender_role="SELLER", direction="OUT", sent_at=now - timedelta(hours=1), body="hi"))
            db.add(Message(store_id="STORE_A", ebay_message_id=f"{prefix}{i}-3", conversation_id=c.id, conversation_seq=3,
                           sender_role="BUYER", direction="IN", sent_at=now - timedelta(minutes=30), body=f"again {i}"))
            j = ReplyJob(store_id="STORE_A", conversation_id=c.id, anchor_source_message_id=f"{prefix}{i}-3",
                         state="REVIEW_REQUIRED" if i % 2 == 0 else "SENT")
            db.add(j)
            db.flush()
            if i % 2 == 0:
                db.add(Draft(job_id=j.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="x",
                             intent="shipping_status"))
            if i % 3 == 0 and tag:
                db.add(ConversationMark(conversation_id=c.id, store_id="STORE_A", tag_id=tag.id, marked_by="t"))
            if i % 4 == 0:
                db.add(OutboundMessage(store_id="STORE_A", conversation_id=c.id, body="b", source="manual", actor="t",
                                       state="PENDING" if i % 8 == 0 else "FAILED", send_after=now))
            if i % 5 == 0:
                db.add(WorkClaim(store_id="STORE_A", conversation_id=c.id, claimed_by="tester", expires_at=now + timedelta(minutes=5)))
            if i % 6 == 0:
                db.add(InvoiceRequest(conversation_id=c.id, store_id="STORE_A", status="OPEN"))
            if i % 7 == 0:
                db.add(BuyerBlacklist(buyer=f"{prefix.lower()}buyer{i}", store_id=None, reason="r", created_by="t"))


class Counter:
    def __init__(self):
        from app.db import get_engine

        self.n = 0
        self.engine = get_engine()
        event.listen(self.engine, "before_cursor_execute", self._hit)

    def _hit(self, *a, **k):
        self.n += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._hit)


def list_sql(client, **params):
    c = Counter()
    try:
        r = client.get("/internal/conversations", params={"group": "all", "kind": "buyer", **params})
        assert r.status_code == 200, r.text
        return c.n, r.json()
    finally:
        c.close()


def test_list_sql_count_constant(client):
    seed(10, "S")
    n10, items10 = list_sql(client)
    seed(90, "T")
    n100, items100 = list_sql(client)
    assert len(items10) == 10 and len(items100) == 100
    assert n10 == n100, (n10, n100)  # 和会话数无关
    assert n100 <= 30  # 含登录校验等每个请求固定的查询
    # 字段照常带上
    one = next(i for i in items100 if i["buyer_key"] == "sbuyer0")
    assert one["intent"] == "shipping_status" and one["claim"]["claimed_by"] == "tester" and one["outbox_pending"] == 1
    assert one["invoice_status"] == "OPEN" and one["blacklist"]["buyer"] == "sbuyer0" and one["marks"] and one["mark"] == one["marks"][0]
    assert one["preview"] == "again 0" and one["preview_role"] == "BUYER" and one["last_replier"] == "本系统"
    assert one["waiting_seconds"] is not None and one["item_url"]
    four = next(i for i in items100 if i["buyer_key"] == "sbuyer4")
    assert four["outbox_problem"] and four["outbox_pending"] == 0 and four["intent"] == "shipping_status"
    three = next(i for i in items100 if i["buyer_key"] == "sbuyer3")
    assert three["intent"] is None and three["claim"] is None and three["blacklist"] is None


def test_draft_filter_applies_to_every_conversation(client):
    """原来循环里把筛选参数 draft 覆盖成了草稿对象，从第二个会话起“有草稿 / 没草稿”筛选失效。"""
    seed(6, "D")
    _, has = list_sql(client, draft="has")
    _, none = list_sql(client, draft="none")
    assert sorted(i["buyer_key"] for i in has) == ["dbuyer0", "dbuyer2", "dbuyer4"]
    assert sorted(i["buyer_key"] for i in none) == ["dbuyer1", "dbuyer3", "dbuyer5"]


def test_version_unchanged_then_changed(client):
    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message
    from app.models import Conversation, ConversationMark, MessageTag, WorkClaim

    seed(3, "V")
    v1 = client.get("/internal/conversations/version").json()
    v2 = client.get("/internal/conversations/version", params={"since": v1["version"]}).json()
    assert v2 == {"version": v1["version"], "changed": False}

    with session_scope() as db:  # 新买家消息
        c = db.query(Conversation).filter_by(ebay_conversation_id="V1").one()
        _append_message(db, c, "V1-new", "BUYER", "IN", "news?", None)
        cid = c.id
    v3 = client.get("/internal/conversations/version", params={"since": v1["version"]}).json()
    assert v3["changed"] is True and v3["version"] != v1["version"]

    def changed_after(fn):
        before = client.get("/internal/conversations/version").json()["version"]
        with session_scope() as db:
            fn(db)
        return client.get("/internal/conversations/version", params={"since": before}).json()["changed"]

    assert changed_after(lambda db: db.add(WorkClaim(store_id="STORE_A", conversation_id=cid, claimed_by="x",
                                                     expires_at=utcnow() + timedelta(minutes=5))))
    tag_id = None
    with session_scope() as db:
        tag_id = db.query(MessageTag).first().id
    assert changed_after(lambda db: db.add(ConversationMark(conversation_id=cid, store_id="STORE_A", tag_id=tag_id, marked_by="x")))
    assert changed_after(lambda db: setattr(db.get(Conversation, cid), "starred", True))
    assert changed_after(lambda db: setattr(db.get(Conversation, cid), "handoff_state", "HUMAN"))
    assert not changed_after(lambda db: None)
    # 只看某个店铺：别的店铺有变化不算
    before = client.get("/internal/conversations/version", params={"store_id": "STORE_A"}).json()["version"]
    with session_scope() as db:
        c = Conversation(store_id="STORE_B", ebay_conversation_id="VB", buyer_key="vb", conversation_type="FROM_MEMBERS")
        db.add(c)
    assert client.get("/internal/conversations/version", params={"store_id": "STORE_A", "since": before}).json()["changed"] is False


def test_version_is_one_query(client):
    seed(5, "Q")
    c = Counter()
    try:
        client.get("/internal/conversations/version")
        base = c.n
    finally:
        c.close()
    seed(50, "R")
    c = Counter()
    try:
        client.get("/internal/conversations/version")
        assert c.n == base
    finally:
        c.close()


def test_missing_indexes_created(client):
    from sqlalchemy import inspect, text

    from app.db import ADD_INDEXES, add_missing_indexes, get_engine

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text('DROP INDEX IF EXISTS "ix_messages_conv_seq"'))
    assert "ix_messages_conv_seq" in add_missing_indexes()
    names = {ix["name"] for t in {x[0] for x in ADD_INDEXES} for ix in inspect(eng).get_indexes(t)}
    assert {x[1] for x in ADD_INDEXES} <= names
    assert add_missing_indexes() == []  # 已有就跳过
