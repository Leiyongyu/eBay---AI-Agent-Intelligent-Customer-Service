"""回复语言跟买家最新一条来信走；答错语言拦下；追问类回复不因 missing_facts 被当成“缺事实”。"""
from app.guards.language import language_mismatch, mostly_chinese, reply_language_hint
from test_auto_reply import gate
from test_pipeline import detail, draft_json, use_fake_llm


def test_hint_and_mismatch():
    assert mostly_chinese("我想要开发票") and not mostly_chinese("I want an invoice")
    assert "中文" in reply_language_hint("我想要开发票")
    assert "最新一条" in reply_language_hint("Ich brauche eine Rechnung")
    assert language_mismatch("我想要开发票", "Hello, your invoice request was forwarded.")
    assert not language_mismatch("我想要开发票", "您好，开票请求已转给财务同事。")
    assert language_mismatch("Where is my order?", "您好，订单已发货。")
    assert not language_mismatch("Wo ist meine Bestellung?", "Hallo, Ihre Bestellung ist unterwegs.")


def test_clarify_missing_facts_not_blocking():
    general = dict(auto_send_intents={"*"}, auto_exclude_intents={"fitment", "price_offer"}, auto_max_risk="L1",
                   evidence_mode="any", kb_evidence=0, auto_clarify_intents={"fitment"})
    ask = gate(intents=["fitment"], action="clarify", missing_facts=["VIN", "engine code"], claims=0, **general)
    assert ask.decision == "AUTO_ELIGIBLE"  # 追问买家要 VIN：要问的内容不算缺事实
    answer = gate(intents=["shipping_status"], action="answer", missing_facts=["tracking"], claims=0, **general)
    assert "MISSING_FACTS" in answer.reasons  # 直接作答却缺事实：仍要人工


def test_chinese_buyer_gets_flagged_english_reply(client):
    from app.ingestion.service import get_or_create_conversation, ingest_buyer_message
    from app.db import session_scope
    from app.worker import worker

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "ZH-1", "buyer_zh", None, "EBAY_US", "simulator")
        ingest_buyer_message(db, conv=conv, ebay_message_id="SIM-IN-ZH-1", body="我想要开发票", skip_merge_window=True,
                             actor="test")
        cid = conv.id
    use_fake_llm(draft_json(claims=[], reply_text="Hello, your invoice request was forwarded."))
    worker.run_once()
    d = detail(client, cid)
    assert "LANGUAGE_MISMATCH" in d["job"]["decision_reasons"]
