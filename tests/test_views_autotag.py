"""买家消息列表三个页签（待审核 / 自动回复 / 全部）与按问题类型自动打标记。"""
from test_conversation_marks import conv


def _job(cid, state, auto=False, anchor="1"):
    from app.db import session_scope
    from app.models import Draft, ReplyJob, SendIntent
    from app.sending.service import AUTO_ACTOR

    with session_scope() as db:
        job = ReplyJob(store_id="STORE_A", conversation_id=cid, anchor_source_message_id=f"SIM-{cid}-{anchor}", state=state)
        db.add(job)
        db.flush()
        if state == "SENT":
            d = Draft(job_id=job.id, run_generation=1, conversation_row_version=1, text="ok", status="APPROVED")
            db.add(d)
            db.flush()
            db.add(SendIntent(job_id=job.id, draft_id=d.id, idempotency_key=f"k{job.id}",
                              actor=AUTO_ACTOR if auto else "tester", text="ok", content_hash="x", state="SENT"))


def test_views_pending_auto_all_and_counts(client):
    review, auto, human, running = (conv("STORE_A", k) for k in ("V1", "V2", "V3", "V4"))
    _job(review, "REVIEW_REQUIRED")
    _job(auto, "SENT", auto=True)
    _job(human, "SENT")
    _job(running, "RUNNING")

    def ids(view, **kw):
        r = client.get("/internal/conversations", params={"group": "all", "kind": "buyer", "view": view, **kw})
        return {i["id"] for i in r.json()}

    assert ids("pending") == {review}
    assert ids("auto") == {auto}
    assert {review, auto, human, running} <= ids("all")
    counts = client.get("/internal/conversations/filter-counts").json()
    assert counts["views"]["pending"] == 1 and counts["views"]["auto"] == 1 and counts["views"]["all"] >= 4

    # 页签里再按标记细分；下拉的数量按当前页签统计
    fit = next(t for t in client.get("/internal/message-tags").json() if t["name"] == "已好评")
    for cid in (review, auto):
        client.put(f"/internal/conversations/{cid}/mark", json={"tag_id": fit["id"]})
    assert ids("pending", tag_id=fit["id"]) == {review}
    c = client.get("/internal/conversations/filter-counts", params={"view": "pending"}).json()
    assert c["all"] == 1 and c["tags"][str(fit["id"])] == 1

    # 自动回复的会话买家又来信、需要人工审核：自动进到待审核
    _job(auto, "REVIEW_REQUIRED", anchor="2")
    assert ids("pending") == {review, auto} and ids("auto") == set()


def test_auto_tags_by_intent(client):
    from app import auto_tags
    from app.db import session_scope
    from app.models import Conversation

    cid = conv("STORE_A", "T1")
    with session_scope() as db:
        added = auto_tags.apply(db, db.get(Conversation, cid), ["fitment", "shipping_status", "delivery_delay", "thanks"])
    assert added == ["A待确认适配", "查物流"]
    marks = client.get(f"/internal/conversations/{cid}").json()["conversation"]["marks"]
    assert [(m["tag"], m["marked_by"]) for m in marks] == [("查物流", auto_tags.ACTOR), ("A待确认适配", auto_tags.ACTOR)]  # 按标签排序
    with session_scope() as db:  # 已有的不重复打
        assert auto_tags.apply(db, db.get(Conversation, cid), ["fitment"]) == []

    # 客服手动去掉的，这个会话以后不再自动打；别的会话照打
    fit = next(m for m in marks if m["tag"] == "A待确认适配")
    client.delete(f"/internal/conversations/{cid}/mark", params={"tag_id": fit["tag_id"]})
    other = conv("STORE_A", "T2")
    with session_scope() as db:
        assert auto_tags.apply(db, db.get(Conversation, cid), ["fitment"]) == []
        assert auto_tags.apply(db, db.get(Conversation, other), ["fitment"]) == ["A待确认适配"]

    # 只打已有的标记项，不自动新建：删掉的、对应不上的、停用的都不打
    client.delete(f"/internal/message-tags/{fit['tag_id']}")
    from sqlalchemy import func, select

    from app.models import MessageTag

    t3, t5 = conv("STORE_A", "T3"), conv("STORE_A", "T5")
    with session_scope() as db:
        before = db.scalar(select(func.count(MessageTag.id)))
        assert auto_tags.apply(db, db.get(Conversation, t3), ["fitment", "price_offer", "item_spec"]) == ["议价"]
        assert db.scalar(select(func.count(MessageTag.id))) == before
        db.scalar(select(MessageTag).where(MessageTag.name == "要发票")).is_active = False
        assert auto_tags.apply(db, db.get(Conversation, t5), ["invoice_vat", "address_change"]) == ["地址问题"]


def test_auto_tags_switch_off(client, monkeypatch):
    from app import auto_tags, config
    from app.db import session_scope
    from app.models import Conversation

    monkeypatch.setenv("AUTO_TAG_ENABLED", "false")
    config.get_settings.cache_clear()
    with session_scope() as db:
        assert auto_tags.apply(db, db.get(Conversation, conv("STORE_A", "T4")), ["fitment"]) == []


def test_situation_label_from_draft(client):
    """AI 起草时给出的会话阶段和一句话概括存到会话上，列表、详情里显示，可按阶段筛选和计数。"""
    from test_generate_draft import make_conversation
    from test_pipeline import draft_json, use_fake_llm
    from app.worker import worker

    cid = make_conversation(["BUYER"])
    use_fake_llm(draft_json(situation_stage="aftersales", situation_zh="收到破损件，  要求补发，等买家发照片确认破损位置"))
    assert client.post(f"/internal/conversations/{cid}/generate-draft").status_code == 200
    worker.run_once()
    s = client.get(f"/internal/conversations/{cid}").json()["conversation"]["situation"]
    assert s["stage"] == "aftersales" and s["label"] == "收到破损件， 要求补发，等买家发照片确认破损位置"[:24]
    ids = lambda stage: {i["id"] for i in client.get("/internal/conversations", params={"group": "all", "stage": stage}).json()}
    assert cid in ids("aftersales") and cid not in ids("presale")
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == cid)
    assert card["situation"]["stage"] == "aftersales"
    assert client.get("/internal/conversations/filter-counts").json()["stages"]["aftersales"] == 1


def test_sidebar_boxes_filter_and_counts(client):
    """左侧菜单的消息箱：已处理 / 升级的消息 / 发送失败 / 暂未分配 / 我认领的。"""
    from app.db import session_scope, utcnow
    from app.models import Conversation, OutboundMessage

    done, handed, failed, mine = (conv("STORE_A", k) for k in ("B1", "B2", "B3", "B4"))
    _job(done, "SENT")
    with session_scope() as db:
        db.get(Conversation, handed).handoff_state = "HUMAN"
        db.add(OutboundMessage(store_id="STORE_A", conversation_id=failed, body="x", source="MANUAL", actor="tester",
                               state="FAILED", send_after=utcnow()))
    assert client.post(f"/internal/conversations/{mine}/claim", json={"action": "claim"}).status_code == 200

    def ids(group):
        r = client.get("/internal/conversations", params={"group": group, "kind": "buyer", "view": "all"})
        return {i["id"] for i in r.json()}

    assert done in ids("resolved") and handed not in ids("resolved")
    assert ids("handoff") == {handed}
    assert ids("failed") == {failed}
    assert ids("mine") == {mine}
    assert mine not in ids("unassigned") and {done, handed, failed} <= ids("unassigned")
    boxes = client.get("/internal/conversations/filter-counts").json()["boxes"]
    assert boxes["handoff"] == 1 and boxes["failed"] == 1 and boxes["mine"] == 1 and boxes["resolved"] >= 1
    assert boxes["unassigned"] >= 3


def test_read_only_and_unread_only_split(client):
    """平台消息的“未处理 / 已处理”：还有未读的和都读过的分开。"""
    unread, read = conv("STORE_A", "R1"), conv("STORE_A", "R2")
    assert client.post(f"/internal/conversations/{read}/read", json={"read": True}).status_code == 200

    def ids(**kw):
        return {i["id"] for i in client.get("/internal/conversations", params={"group": "all", **kw}).json()}

    assert unread in ids(unread_only="true") and read not in ids(unread_only="true")
    assert read in ids(read_only="true") and unread not in ids(read_only="true")
