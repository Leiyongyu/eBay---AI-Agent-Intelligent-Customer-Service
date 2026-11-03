"""AI 自动回复：知识库里有固定答案、低风险的自动（模拟）发出，其余留给人工；按店铺开关自动触发。"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.guards import risk
from test_ebay_sync import CONVS, MESSAGES, FakeEbay, ebay_env, make_sync  # noqa: F401  ebay_env 是夹具
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm

KB = "kb:KB-SHIP@1"


def gate(**kw):
    base = dict(intents=["shipping_status"], buyer_text="Where is my order?", marketplace_id="EBAY_US",
                action="answer", auto_send_enabled=True, store_auto_send_enabled=True,
                auto_send_intents={"shipping_status"}, auto_clarify_intents={"fitment"}, kb_evidence=1)
    base.update(kw)
    return risk.evaluate(risk.GateInput(**base))


def test_gate_requires_kb_evidence_and_limits_streak():
    assert gate().decision == "AUTO_ELIGIBLE"
    assert gate(kb_evidence=0).reasons == ["NO_KB_EVIDENCE"]
    assert gate(previous_auto_reply=True).reasons == ["AUTO_REPLY_STREAK"]


def test_gate_allows_fitment_only_when_asking_for_info():
    ask = gate(intents=["fitment"], action="clarify", buyer_text="Passt das für meinen Golf?")
    assert (ask.risk_level, ask.decision) == ("L1", "AUTO_ELIGIBLE")
    answer = gate(intents=["fitment"], action="answer", buyer_text="Passt das für meinen Golf?")
    assert {"RISK_NOT_L0", "INTENT_NOT_WHITELISTED"} <= set(answer.reasons)
    other = gate(intents=["other"], action="clarify", buyer_text="hello")  # 无法归类的不放行
    assert "RISK_NOT_L0" in other.reasons
    mixed = gate(intents=["fitment", "return_refund"], action="clarify", buyer_text="Passt das? Sonst Rückgabe")
    assert mixed.decision == "REVIEW_REQUIRED"


@pytest.fixture()
def auto_env(client, monkeypatch):
    from app.db import session_scope
    from app.models import KnowledgeChunk

    monkeypatch.setenv("AUTO_SEND_ENABLED", "true")
    monkeypatch.setenv("AUTO_SEND_INTENTS", "shipping_status,thanks_closing")
    monkeypatch.setenv("AUTO_SEND_CLARIFY_INTENTS", "fitment")
    config.get_settings.cache_clear()
    with session_scope() as db:
        db.add(KnowledgeChunk(
            chunk_id="KB-SHIP", clause_id="KB-SHIP", document_id="T", version="1", title="发货与物流单号",
            text="发货后在 eBay 订单里上传物流单号，买家可在订单详情查看物流。", store_id="ALL", marketplace_id="ALL",
            audience="PUBLIC_REPLY", language="zh", topic="物流", priority=20,
            effective_at=datetime(2026, 1, 1).date(), review_state="PUBLISHED", sku=[], source="tests",
            content_hash="kb-ship"))
    return client


def kb_draft(**kw):
    claims = [{"text": "The tracking number is shown in your eBay order details.", "evidence_ids": [KB]}]
    return draft_json(claims=claims, reply_text="Hi, you can find the tracking number in your eBay order details.",
                      reply_zh="您好，物流单号可以在 eBay 订单详情里查看。", **kw)


def test_store_switch_auto_sends_kb_answer_then_hands_over(auto_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ReviewFeedback
    from app.worker import worker

    client = auto_env
    # 店铺开关没开：同样的草稿只进待审核
    use_fake_llm(kb_draft())
    first = send_buyer(client, "Where is my order?", buyer="buyer_auto_1")
    worker.run_once()
    d = detail(client, first["conversation_id"])
    assert (d["job"]["state"], d["job"]["decision_reasons"]) == ("REVIEW_REQUIRED", ["STORE_AUTO_SEND_OFF"])
    assert d["messages"][-1]["role"] == "BUYER"

    r = client.post("/internal/stores/STORE_A/auto-reply", json={"enabled": True})
    assert r.json() == {"store_id": "STORE_A", "auto_send_enabled": True}
    assert client.get("/internal/auto-reply/settings").json() == {
        "auto_send_enabled": True, "intents": ["shipping_status", "thanks_closing"], "clarify_intents": ["fitment"],
        "listing_intents": [], "exclude_intents": [], "max_risk": "L0", "evidence": "kb", "max_streak": 1}

    use_fake_llm(kb_draft())
    sent = send_buyer(client, "Where is my order?", buyer="buyer_auto_2")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    last = d["messages"][-1]
    assert d["job"]["state"] == "SENT"
    assert last["role"] == "SELLER" and last["simulated"] and last["trace"]["auto"] is True
    assert last["zh_translation"] == "您好，物流单号可以在 eBay 订单详情里查看。"
    items = client.get("/internal/conversations", params={"group": "auto"}).json()
    assert [i["id"] for i in items] == [sent["conversation_id"]] and items[0]["auto_replied"]
    with session_scope() as db:
        assert db.scalars(select(ReviewFeedback)).all() == []  # 自动回复不算人工审核

    # 买家接着再问：上一条已是自动回复，这次交人工
    use_fake_llm(kb_draft(intent="thanks_closing", intents=["thanks_closing"]))
    send_buyer(client, "Thanks! And when exactly?", buyer="buyer_auto_2")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "AUTO_REPLY_STREAK" in d["job"]["decision_reasons"]

    # 没有引用知识库的回复不自动发
    use_fake_llm(draft_json())
    other = send_buyer(client, "Where is my order?", buyer="buyer_auto_3")
    worker.run_once()
    d = detail(client, other["conversation_id"])
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "NO_KB_EVIDENCE" in d["job"]["decision_reasons"]


def test_claimed_conversation_is_not_auto_sent(auto_env):
    from app.worker import worker

    client = auto_env
    client.post("/internal/stores/STORE_A/auto-reply", json={"enabled": True})
    use_fake_llm(kb_draft())
    sent = send_buyer(client, "Where is my order?", buyer="buyer_auto_4")
    client.post(f"/internal/conversations/{sent['conversation_id']}/claim", json={"action": "claim"})
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "AUTO_ELIGIBLE" and d["messages"][-1]["role"] == "BUYER"


def test_sync_creates_jobs_only_for_recent_messages_of_enabled_store(ebay_env, monkeypatch):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ReplyJob, Store

    convs, msgs = copy.deepcopy(CONVS), copy.deepcopy(MESSAGES)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fresh = {"messageId": "M9", "senderUsername": "buyer_c", "recipientUsername": "stardrive_real",
             "messageBody": "Still there?", "createdDate": now, "subject": "", "readStatus": False}
    msgs["C3"].append(fresh)
    convs["FROM_MEMBERS"][2]["latestMessage"] = fresh
    monkeypatch.setattr("test_ebay_sync.CONVS", convs)
    monkeypatch.setattr("test_ebay_sync.MESSAGES", msgs)

    assert make_sync(FakeEbay()).run()["ok"]  # 店铺开关没开：不建任务
    with session_scope() as db:
        assert db.scalars(select(ReplyJob)).all() == []
        db.get(Store, "REAL_US").auto_send_enabled = True
        db.execute(ReplyJob.__table__.delete())
    with session_scope() as db:  # 删掉已入库的消息，模拟这些消息是开关打开后才拉到的
        from app.models import Conversation, Message

        db.execute(Message.__table__.delete())
        db.execute(Conversation.__table__.delete())
    assert make_sync(FakeEbay()).run()["ok"]
    with session_scope() as db:
        jobs = db.scalars(select(ReplyJob)).all()
        assert len(jobs) == 1  # 只有 24 小时内的 M9；前天的旧买家消息不自动处理


def test_gate_general_mode_l1_any_evidence_with_exclusions():
    general = dict(auto_send_intents={"*"}, auto_exclude_intents={"fitment", "price_offer"}, auto_max_risk="L1",
                   evidence_mode="any", kb_evidence=0)
    # 普通 L1 问题（发货时效）按订单/物流等查询结果作答：自动发
    assert gate(intents=["handling_time"], other_evidence=1, claims=1, **general).decision == "AUTO_ELIGIBLE"
    # 寒暄、没有事实陈述的回复：自动发
    assert gate(intents=["other"], action="clarify", buyer_text="hello", claims=0, **general).decision == "AUTO_ELIGIBLE"
    # 有事实陈述却没有任何依据：转人工
    assert "NO_KB_EVIDENCE" in gate(intents=["stock"], claims=1, **general).reasons
    # 排除的意图：适配结论、议价仍转人工；只索要 VIN 的适配追问照常自动发
    assert "INTENT_NOT_WHITELISTED" in gate(intents=["fitment"], other_evidence=1, claims=1, **general).reasons
    assert "INTENT_NOT_WHITELISTED" in gate(intents=["price_offer"], other_evidence=1, claims=1, **general).reasons
    assert gate(intents=["fitment"], action="clarify", claims=0, **general).decision == "AUTO_ELIGIBLE"
    # L2（退货退款等）任何配置下都不自动发；模型要求人工、缺事实也不发
    assert "RISK_NOT_L0" in gate(intents=["return_refund"], buyer_text="refund?", other_evidence=1, claims=1,
                                 **general).reasons
    assert "MISSING_FACTS" in gate(intents=["shipping_status"], missing_facts=["tracking"], claims=0, **general).reasons


def test_streak_limit_counts_consecutive_auto_replies(client, monkeypatch):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.sending import service

    monkeypatch.setattr(service, "auto_reply_message_ids", lambda db, ids: {"SIM-3", "SIM-5"} & set(ids))
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "STREAK", "b_streak", None, "EBAY_US", "simulator")
        for i, role in enumerate(["SELLER", "BUYER", "SELLER", "BUYER", "SELLER"], 1):
            _append_message(db, conv, f"SIM-{i}", role, "OUT" if role == "SELLER" else "IN", "x", None)
        db.flush()
        assert service.auto_reply_streak(db, conv.id) == 2  # 末尾两条店铺回复是自动的，第一条是人工
