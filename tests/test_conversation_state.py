"""会话状态卡 + 意图把握度和依据（假模型多轮场景）：
先说没收到 → 我们回复 → 再问“有进展吗”仍判妥投未收到并接上查件单；中途换话题问适配；一条消息两件事；
把握度低转 clarify + 审核原因；状态卡合并规则（系统事实覆盖模型输出、模型漏掉的问题算已解决、长度上限）。"""
from dataclasses import replace

import pytest
from sqlalchemy import select

from test_pipeline import draft_json, send_buyer, use_fake_llm

BUYER, ITEM, ORDER = "buyer_us_002", "110000000001", "26-10002-US"  # 演示数据：这单已签收


@pytest.fixture()
def delivered(monkeypatch):
    """查包裹固定返回“已签收”（演示数据没有轨迹）。"""
    from app.tools import registry

    calls = []

    def fake(ctx, src, args, db=None):
        calls.append(args.order_id)
        return "FOUND", {"order_id": args.order_id, "carrier": "USPS", "tracking_number": "9400100000000000010002",
                         "tracking": {"status": "Delivered", "events": []},
                         "assessment": {"stage": "DELIVERED", "stage_label": "已签收", "needs_human": False}}, \
            f"package:{args.order_id}", None

    monkeypatch.setitem(registry.TOOLS, "get_package_status", replace(registry.TOOLS["get_package_status"], fn=fake))
    return calls


def last_buyer_id(conv_id):
    from app.db import session_scope
    from app.models import Message

    with session_scope() as db:
        return db.scalars(select(Message.ebay_message_id).where(Message.conversation_id == conv_id, Message.sender_role == "BUYER")
                          .order_by(Message.conversation_seq.desc())).first()


def run_round(client, text, **out):
    """买家来一条 → 假模型按 out 起草 → 返回（会话 ID、本条消息 ID、任务、草稿）。out 里的 basis 用 "SELF" 指本条消息。"""
    from app.db import session_scope
    from app.models import Draft, ReplyJob
    from app.worker import worker

    sent = send_buyer(client, text, buyer=BUYER, item=ITEM)
    mid = last_buyer_id(sent["conversation_id"])
    if "intent_basis" in out:
        out["intent_basis"] = [mid if x == "SELF" else x for x in out["intent_basis"]]
    use_fake_llm(draft_json(claims=[], **out))
    assert worker.run_once()
    with session_scope() as db:
        job = db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == sent["conversation_id"])
                         .order_by(ReplyJob.id.desc())).first()
        draft = db.scalars(select(Draft).where(Draft.job_id == job.id).order_by(Draft.id.desc())).first()
        db.expunge_all()
    return sent["conversation_id"], mid, job, draft


def seller_reply(conv_id, text):
    """店铺回复了买家（和真实发送一样：会话里多一条店铺消息，之前的回复任务结束）。"""
    from app.db import session_scope
    from app.ingestion.service import _append_message
    from app.models import Conversation, JobState, ReplyJob

    with session_scope() as db:
        _append_message(db, db.get(Conversation, conv_id), f"OUT-{text[:8]}", "SELLER", "OUT", text, None)
        for j in db.scalars(select(ReplyJob).where(ReplyJob.conversation_id == conv_id, ReplyJob.state.in_(JobState.REPLACEABLE))):
            j.state = JobState.SENT


def card_of(conv_id):
    from app.db import session_scope
    from app.models import Conversation

    with session_scope() as db:
        return db.get(Conversation, conv_id).state_card


def test_follow_up_inherits_not_received_and_ticket(client, delivered):
    from app.db import session_scope, utcnow
    from app.models import AgentStep, AgentRun, LogisticsTicket

    # 第 1 轮：买家说签收了但没收到
    cid, m1, job1, d1 = run_round(
        client, "Tracking says delivered but I have not received my parcel.", intent="delivery_delay",
        intents=["delivery_delay"], action="handoff", needs_human=True, intent_confidence="high", intent_basis=["SELF"],
        state_update={"topic": "签收未收到", "open_issues": [{"intent": "delivery_delay", "summary_zh": "显示签收但没收到",
                                                               "order_id": ORDER, "status_zh": "转人工"}],
                      "promises": ["同事会尽快跟进"], "facts": {"order_ids": [ORDER]}})
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job1.decision_reasons
    assert d1.intent_confidence == "high" and d1.intent_basis == [m1] and d1.inherits_issue is None
    card = card_of(cid)
    # 这一轮系统已自动登记了“妥投未收到”的异常包裹跟进：模型列的同一件事以系统为准；eBay 退货也作为系统问题列出
    parcel = next(i for i in card["issues"] if i["id"].startswith("sys:parcel:"))
    assert "NOT_RECEIVED" in parcel["flags"] and parcel["order_id"] == ORDER
    assert any(i["id"] == "sys:case:RETURN:RET-5000123" for i in card["issues"])
    assert not [i for i in card["issues"] if i["source"] == "model"]
    assert card["promises"] == ["同事会尽快跟进"] and card["facts"]["order_ids"] == [ORDER] and card["topic"] == "签收未收到"

    # 同事回复了买家，并提交了“妥投未收到”查件单
    seller_reply(cid, "Sorry to hear that, we are checking with the carrier.")
    with session_scope() as db:
        db.add(LogisticsTicket(io_code="CJTEST1", conversation_id=cid, store_id="STORE_A", reference_no=ORDER,
                               type_name="妥投未收到", io_status=3, submitted_at=utcnow(), added_at="2026-11-09 10:00:00"))

    # 第 2 轮：只问“有进展吗”——没有物流词，也照样查包裹、判妥投未收到，并接上查件单
    delivered.clear()
    cid2, m2, job2, d2 = run_round(
        client, "Any news?", intent="delivery_delay", intents=["delivery_delay"], action="answer", intent_confidence="medium",
        inherits_issue="sys:ticket:CJTEST1", intent_basis=[m1, "SELF", "NOT-A-REAL-ID"],
        state_update={"topic": "签收未收到，等查件", "open_issues": []})
    assert cid2 == cid and delivered == [ORDER]  # 状态卡里有未结物流问题：照样查了包裹
    with session_scope() as db:  # 这一轮只覆盖“有进展吗”这一条（上一轮已回复）：判妥投未收到靠的是状态卡
        from app.models import ReplyJobSource

        assert list(db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == job2.id))) == [m2]
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job2.decision_reasons
    assert d2.inherits_issue == "sys:ticket:CJTEST1" and d2.intent_basis == [m1, m2]  # 不存在的消息 ID 去掉
    card = card_of(cid)
    sys_issue = next(i for i in card["issues"] if i["id"] == "sys:ticket:CJTEST1")
    assert sys_issue["source"] == "system" and "NOT_RECEIVED" in sys_issue["flags"] and sys_issue["refs"] == ["CJTEST1"]
    assert not [i for i in card["issues"] if i["source"] == "model"]  # 和系统查件单是同一件事：以系统为准
    with session_scope() as db:  # 起草前带进提示词的状态卡里就有这张查件单
        run = db.scalars(select(AgentRun).where(AgentRun.job_id == job2.id)).first()
        step = db.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node == "load_memory")).first()
        assert "sys:ticket:CJTEST1" in step.detail["state_issues"]

    # 工作台：草稿和生成依据里能看到意图判断和状态卡
    d = client.get(f"/internal/conversations/{cid}").json()
    assert d["agent_draft"]["inherits_issue"] == "sys:ticket:CJTEST1" and d["agent_draft"]["intent_confidence"] == "medium"
    assert any(i["id"] == "sys:ticket:CJTEST1" for i in d["state_card"]["issues"])

    # 第 3 轮：中途换话题问适配——不再按“没收到”处理
    seller_reply(cid, "We have contacted the carrier and will update you.")
    _, m3, job3, d3 = run_round(
        client, "Different question: does this sensor fit a 2015 VW Golf 7 1.6 TDI?", intent="fitment", intents=["fitment"],
        action="clarify", intent_confidence="high", intent_basis=["SELF"],
        state_update={"topic": "问 Golf 7 适配", "open_issues": [{"intent": "fitment", "summary_zh": "问 Golf 7 适配"}],
                      "facts": {"vehicle": "VW Golf 7 2015 1.6 TDI"}})
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job3.decision_reasons and d3.inherits_issue is None
    card = card_of(cid)
    assert card["facts"]["vehicle"] == "VW Golf 7 2015 1.6 TDI" and card["topic"] == "问 Golf 7 适配"
    assert {i["id"] for i in card["issues"]} >= {"sys:ticket:CJTEST1", "m:fitment:"}  # 系统问题模型删不掉


def test_two_things_in_one_message(client, delivered):
    _, mid, job, d = run_round(
        client, "Does it fit a 2012 BMW E90? And how much is shipping to Austria?", intent="fitment",
        intents=["fitment", "shipping_cost"], action="clarify", intent_confidence="high", intent_basis=["SELF"],
        state_update={"topic": "问适配和运费", "open_issues": [{"intent": "fitment", "summary_zh": "问 E90 适配"},
                                                             {"intent": "shipping_cost", "summary_zh": "问奥地利运费"}]})
    assert d.intents == ["fitment", "shipping_cost"]
    card = card_of(job.conversation_id)
    assert {i["id"] for i in card["issues"] if i["source"] == "model"} == {"m:fitment:", "m:shipping_cost:"}


def test_low_confidence_goes_clarify_with_reason(client, delivered):
    _, _, job, d = run_round(client, "It still doesn't work.", intent="other", intents=["other"], action="clarify",
                             intent_confidence="low", intent_basis=["SELF"])
    assert "INTENT_UNCERTAIN" in job.decision_reasons and job.state == "REVIEW_REQUIRED"
    assert d.action == "clarify" and d.intent_confidence == "low"
    from app.guards.risk import REASON_LABELS

    assert "INTENT_UNCERTAIN" in REASON_LABELS


def test_merge_rules_system_wins_and_resolution():
    from app import conversation_state as cs

    sys_issues = [{"id": "sys:ticket:CJ9", "source": "system", "intent": "delivery_delay", "summary_zh": "谷仓查件（妥投未收到）",
                   "order_id": "01-1", "since": "2026-11-08", "status": "等承运商回复", "refs": ["CJ9"],
                   "flags": ["LOGISTICS", "NOT_RECEIVED"]}]
    before = {"topic": "旧主题", "issues": [
        {"id": "m:fitment:", "source": "model", "intent": "fitment", "summary_zh": "问适配", "order_id": None,
         "since": "2026-11-01", "status": "", "refs": [], "flags": []},
        {"id": "m:invoice_vat:", "source": "model", "intent": "invoice_vat", "summary_zh": "要发票", "order_id": None,
         "since": "2026-11-02", "status": "", "refs": [], "flags": ["INVOICE"]}],
        "resolved": [], "facts": {"vin": "WVWZZZ1KZ6W000001"}, "promises": ["等查件结果"]}
    update = {"topic": "等查件", "promises": None, "resolved": ["运费已答复"],
              "open_issues": [{"intent": "delivery_delay", "summary_zh": "没收到包裹", "order_id": "01-1"},  # 和系统查件单重复
                              {"intent": "fitment", "summary_zh": "还在等 VIN"}],
              "facts": {"skus": ["SKU-1"]}}
    card = cs.merge(before, update, sys_issues, {"order_ids": ["01-1"], "media": "已收到买家图片 2 张"}, intent="delivery_delay")
    ids = [i["id"] for i in card["issues"]]
    assert ids == ["sys:ticket:CJ9", "m:fitment:"]  # 重复的以系统为准；模型没再列的发票 → 已解决
    assert card["issues"][1]["since"] == "2026-11-01" and card["issues"][1]["summary_zh"] == "还在等 VIN"
    assert [r["summary_zh"] for r in card["resolved"]] == ["要发票", "运费已答复"]
    assert card["promises"] == ["等查件结果"]  # 模型没给 promises：保留原来的
    assert card["facts"] == {"vin": "WVWZZZ1KZ6W000001", "skus": ["SKU-1"], "order_ids": ["01-1"], "media": "已收到买家图片 2 张"}

    # 模型明说沿用的问题，即使漏写也不会被当成已解决；规则标记挂到它上面
    card2 = cs.merge(card, {"open_issues": []}, [], {}, inherits="m:fitment:", intent="fitment", rule_flags={"RESHIP"})
    fit = next(i for i in card2["issues"] if i["id"] == "m:fitment:")
    assert "RESHIP" in fit["flags"]

    # 系统问题不在模型手里：模型说全解决了，系统问题照样在
    card3 = cs.merge(card, {"open_issues": [], "resolved": ["都解决了"]}, sys_issues, {})
    assert [i["id"] for i in card3["issues"]] == ["sys:ticket:CJ9"]


def test_card_length_cap_and_carry_rules():
    from app import conversation_state as cs

    many = [{"intent": "other", "summary_zh": f"问题{i}" * 8, "order_id": f"0{i}-12345-67890", "status_zh": "处理中" * 5}
            for i in range(6)]
    card = cs.merge(cs._empty(), {"topic": "很长" * 30, "open_issues": many, "promises": ["答应" * 30] * 4,
                                  "resolved": ["解决" * 30] * 4}, [], {})
    assert cs.tokens(card) <= cs.MAX_TOKENS and len(card["topic"]) <= 30

    card = {"issues": [{"id": "sys:ticket:A", "source": "system", "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "m:invoice_vat:", "source": "model", "flags": ["INVOICE"]}]}
    assert cs.carried_flags(card, "sys:ticket:A", "Any news?") == {"LOGISTICS", "NOT_RECEIVED"}
    assert cs.carried_flags(card, None, "Gibt es schon Neuigkeiten?") == {"LOGISTICS", "NOT_RECEIVED", "INVOICE"}  # 模型没判：短追问接上
    assert cs.carried_flags(card, None, "Does it fit a Golf 7?") == set()  # 新话题不接
    assert cs.carried_flags(card, "m:gone:", "Danke") == set()


def test_follow_up_fallback_only_carries_issues_related_to_model_intent():
    """短追问兜底：模型判了明确的新意图时只接相关问题；没起草 / 意图只有 other 时接全部。"""
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:ticket:A", "source": "system", "intent": "delivery_delay", "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "sys:invoice", "source": "system", "intent": "invoice_vat", "flags": ["INVOICE"]},
                       {"id": "m:fitment:", "source": "model", "intent": "fitment", "flags": []}]}
    text = "What about the front sensor, does it fit a Golf 7?"
    assert cs.carried_flags(card, None, text, ["fitment"]) == set()  # 判成适配：不接“没收到”
    assert cs.carried_flags(card, None, "Noch nicht angekommen, gibt es Neuigkeiten?", ["delivery_delay"]) == {"LOGISTICS", "NOT_RECEIVED"}
    assert cs.carried_flags(card, None, "Any news?", ["invoice_vat"]) == {"INVOICE"}
    assert cs.carried_flags(card, None, "Any news?", ["other"]) == {"LOGISTICS", "NOT_RECEIVED", "INVOICE"}
    assert cs.carried_flags(card, None, "Any news?", None) == {"LOGISTICS", "NOT_RECEIVED", "INVOICE"}
    assert cs.carried_flags(card, "sys:invoice", "Any news?", ["fitment"]) == {"INVOICE"}  # 模型明说沿用的为准


def test_follow_up_sentences_orders_and_case_intents():
    """E2 / E3 / E4：追问按句子判断；只接订单对得上的问题；退货退款意图只接 eBay 售后单上的标记。"""
    from app import conversation_state as cs

    long_text = ("Hello again, just checking in: any news on the parcel? Also, while I'm writing anyway, could you please tell me "
                 "whether the same sensor also fits my second car, a 2014 Skoda Octavia III 1.6 TDI with the CLHA engine?")
    assert cs.is_follow_up(long_text) and len(long_text) > 160
    assert not cs.is_follow_up("Ich habe mich noch nicht entschieden, ob ich den Sensor für meinen Golf 7 Variant mit dem "
                               "1.6 TDI Motor oder doch lieber das Originalteil vom Händler kaufe, was meinen Sie dazu genau")
    card = {"issues": [{"id": "sys:ticket:A", "source": "system", "intent": "delivery_delay", "order_id": "01-1",
                        "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "sys:case:INQUIRY:9", "source": "system", "intent": "delivery_delay", "order_id": "02-2",
                        "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "m:invoice_vat:", "source": "model", "intent": "invoice_vat", "order_id": None, "flags": ["INVOICE"]}]}
    assert cs.carried_flags(card, None, "Any news?", ["shipping_status"], ["03-3"]) == set()  # 两单都不是这笔
    assert cs.carried_flags(card, None, "Any news?", ["shipping_status"], ["01-1"]) == {"LOGISTICS", "NOT_RECEIVED"}
    assert cs.carried_flags(card, None, "Any news?", None, ["01-1", "02-2"]) == {"INVOICE"}  # 订单不明确：只接没写订单的
    assert cs.carried_flags(card, "sys:case:INQUIRY:9", "Any news?", ["shipping_status"], ["03-3"]) == {"LOGISTICS", "NOT_RECEIVED"}
    # 退货退款意图：接未收到货咨询（售后单），不接查件单上的“没收到”
    assert cs.carried_flags(card, None, "I opened a case. Any news?", ["return_refund"], ["02-2"]) == {"LOGISTICS", "NOT_RECEIVED"}
    assert cs.carried_flags(card, None, "Any news on my refund?", ["refund_status"], ["01-1"]) == set()


def test_legacy_case_order_mapped_through_order_lines(client):
    """E2b：ebay_cases 里咨询 / 纠纷的订单号是旧格式“商品号-交易号”，按订单商品明细换算成新格式再比；
    换算不了的当作没写订单。E3b：德语缩写不算句末。"""
    from app import conversation_state as cs
    from app.db import session_scope, utcnow
    from app.ingestion.service import get_or_create_conversation
    from app.models import EbayCase, EbayOrderLine

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "LEG-1", "buyer_leg", "110000000001", "EBAY_US", "listing")
        db.add(EbayOrderLine(store_id="STORE_A", order_id="26-55555-US", line_item_id="2345678901001",
                             line_key="110000000003-2345678901001", item_id="110000000003", buyer="buyer_leg"))
        for ext, order in (("C1", "110000000003-2345678901001"), ("C2", "119999999999-9999999999999")):
            db.add(EbayCase(store_id="STORE_A", kind="INQUIRY", external_id=ext, buyer="buyer_leg", order_id=order,
                            is_open=True, opened_at=utcnow()))
        db.flush()
        issues = {i["id"]: i for i in cs.system_issues(db, conv)}
    assert issues["sys:case:INQUIRY:C1"]["order_key"] == "26-55555-US"  # 换算到新格式
    assert "order_key" not in issues["sys:case:INQUIRY:C2"]  # 换算不了：当作没写订单
    card = {"issues": list(issues.values())}
    assert "NOT_RECEIVED" in cs.carried_flags(card, None, "Any news?", ["delivery_delay"], ["26-55555-us "])
    only_c1 = {"issues": [issues["sys:case:INQUIRY:C1"]]}
    assert cs.carried_flags(only_c1, None, "Any news?", ["delivery_delay"], ["26-66666-US"]) == set()  # 另一单
    assert not cs.is_follow_up("Ich habe z.B. drei Teile, die ich noch nicht eingebaut habe und die ich gern zusammen mit dem "
                               "neuen Thermostat in meinen Golf 7 Variant Baujahr 2015 einbauen würde.")
    assert cs.is_follow_up("Danke für die Info. Gibt es schon Neuigkeiten? Ich warte ca. zwei Wochen.")
