"""验收（测试工程师，2026-11-09）：会话状态卡 + 意图把握度——补开发用例没覆盖到的点，全部用假模型。

覆盖：德语 / 中文短追问接续并接上查件单和异常包裹跟进；换话题但句子里碰巧有“what about / noch nicht”；
把握度低 + 开了自动回复也不自动发；乱写 / 缺失新字段；再要发票接沿用的发票问题；旧草稿 / 旧会话没有新字段；
schema_instruction 去掉 title 后识图解析；状态卡长度裁剪的精确顺序。"""
import json

import pytest
from sqlalchemy import select

from app import config
from test_conversation_state import BUYER, ITEM, ORDER, card_of, delivered, run_round, seller_reply  # noqa: F401  delivered 是夹具
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm

NR_ROUND1 = dict(
    intent="delivery_delay", intents=["delivery_delay"], action="handoff", needs_human=True, intent_confidence="high",
    intent_basis=["SELF"], state_update={"topic": "签收未收到", "open_issues": [
        {"intent": "delivery_delay", "summary_zh": "显示签收但没收到", "order_id": ORDER, "status_zh": "转人工"}]})


def _not_received_then_ticket(client):
    """第 1 轮买家说签收没收到 → 同事回复 → 提交“妥投未收到”查件单（和真实提交一样同步异常包裹跟进）。"""
    from app import parcel_auto
    from app.db import session_scope, utcnow
    from app.models import LogisticsTicket

    cid, m1, job1, _ = run_round(client, "Tracking says delivered but I have not received my parcel.", **NR_ROUND1)
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job1.decision_reasons
    seller_reply(cid, "Sorry to hear that, we are checking with the carrier.")
    with session_scope() as db:
        t = LogisticsTicket(io_code="CJACC1", conversation_id=cid, store_id="STORE_A", reference_no=ORDER,
                            type_name="妥投未收到", io_status=3, submitted_at=utcnow(), added_at="2026-11-09 10:00:00")
        db.add(t)
        db.flush()
        parcel_auto.on_ticket_submitted(db, t)
    return cid, m1


def _parcels(cid):
    from app.db import session_scope
    from app.models import ParcelFollowup

    with session_scope() as db:
        return [(p.order_id, p.reason, p.status, p.ticket_no) for p in
                db.scalars(select(ParcelFollowup).where(ParcelFollowup.conversation_id == cid))]


# ---------------------------------------------------------------- 3.1 多轮接续（德语 / 中文 / 英文）
@pytest.mark.parametrize("text", ["Any news?", "Gibt es Neuigkeiten?", "还是不行"])
@pytest.mark.parametrize("model_inherits", [True, False], ids=["model_inherits", "model_null"])
def test_short_follow_up_keeps_ticket_and_parcel(client, delivered, text, model_inherits):
    cid, m1 = _not_received_then_ticket(client)
    before = _parcels(cid)
    assert len(before) == 1 and "CJACC1" in (before[0][3] or "")

    delivered.clear()
    cid2, m2, job2, d2 = run_round(
        client, text, intent="delivery_delay", intents=["delivery_delay"], action="answer", intent_confidence="medium",
        inherits_issue="sys:ticket:CJACC1" if model_inherits else None, intent_basis=[m1, "SELF"],
        state_update={"topic": "签收未收到，等查件", "open_issues": []})
    assert cid2 == cid and delivered == [ORDER]  # 仍查包裹
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job2.decision_reasons  # 仍判妥投未收到
    assert d2.inherits_issue == ("sys:ticket:CJACC1" if model_inherits else None)
    assert d2.intent_basis == [m1, m2]
    card = card_of(cid)
    ids = [i["id"] for i in card["issues"]]
    assert "sys:ticket:CJACC1" in ids
    parcel = next(i for i in card["issues"] if i["id"].startswith("sys:parcel:"))
    assert "CJACC1" in parcel["refs"] and "NOT_RECEIVED" in parcel["flags"]  # 查件单和异常包裹跟进接上
    after = _parcels(cid)
    assert len(after) == 1 and after[0][2] == "OPEN" and "CJACC1" in after[0][3]  # 没有重复登记


# ---------------------------------------------------------------- 3.2 换话题（句子里碰巧带“追问”词）
@pytest.mark.parametrize("text", [
    "Does this sensor also fit a 2015 VW Golf 7 1.6 TDI?",
])
def test_topic_change_not_treated_as_not_received(client, delivered, text):
    cid, _ = _not_received_then_ticket(client)
    _, _, job, d = run_round(client, text, intent="fitment", intents=["fitment"], action="clarify",
                             intent_confidence="high", intent_basis=["SELF"],
                             state_update={"topic": "问适配", "open_issues": [{"intent": "fitment", "summary_zh": "问适配"}]})
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job.decision_reasons and d.inherits_issue is None


@pytest.mark.parametrize("text", [
    "What about the front sensor, does it fit a 2015 VW Golf 7?",
    "Ich habe den zweiten noch nicht bestellt. Passt der Sensor auf einen Golf 7?",
    "还没买第二个，这个传感器适配2015款高尔夫7吗？",
], ids=["en_what_about", "de_noch_nicht", "zh_hai_mei"])
def test_topic_change_with_follow_up_words(client, delivered, text):
    cid, _ = _not_received_then_ticket(client)
    _, _, job, d = run_round(client, text, intent="fitment", intents=["fitment"], action="clarify",
                             intent_confidence="high", intent_basis=["SELF"],
                             state_update={"topic": "问适配", "open_issues": [{"intent": "fitment", "summary_zh": "问适配"}]})
    assert d.inherits_issue is None
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job.decision_reasons


def test_two_things_follow_up_and_new_question(client, delivered):
    """一条消息里既追问包裹又问发票：两个意图都列出，仍接上查件单。"""
    cid, m1 = _not_received_then_ticket(client)
    _, _, job, d = run_round(
        client, "Any news on my parcel? Also, can I get an invoice for this order?", intent="delivery_delay",
        intents=["delivery_delay", "invoice_vat"], action="answer", intent_confidence="high",
        inherits_issue="sys:ticket:CJACC1", intent_basis=["SELF"],
        state_update={"open_issues": [{"intent": "invoice_vat", "summary_zh": "要发票"}]})
    assert d.intents == ["delivery_delay", "invoice_vat"]
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons
    ids = {i["id"] for i in card_of(cid)["issues"]}
    assert "sys:ticket:CJACC1" in ids and ({"m:invoice_vat:", "sys:invoice"} & ids)


# ---------------------------------------------------------------- 3.4 把握度低：开了自动回复也不自动发
@pytest.fixture()
def auto_client(client, monkeypatch):
    from datetime import datetime

    from app.db import session_scope
    from app.models import KnowledgeChunk

    monkeypatch.setenv("AUTO_SEND_ENABLED", "true")
    monkeypatch.setenv("AUTO_SEND_INTENTS", "shipping_status")
    monkeypatch.setenv("AUTO_SEND_CLARIFY_INTENTS", "fitment")
    config.get_settings.cache_clear()
    with session_scope() as db:
        db.add(KnowledgeChunk(
            chunk_id="KB-FIT", clause_id="KB-FIT", document_id="T", version="1", title="适配核对",
            text="请提供车辆 VIN 或车型年份排量，我们帮您核对适配。", store_id="ALL", marketplace_id="ALL",
            audience="PUBLIC_REPLY", language="zh", topic="适配", priority=20,
            effective_at=datetime(2026, 1, 1).date(), review_state="PUBLISHED", sku=[], source="tests",
            content_hash="kb-fit"))
    assert client.post("/internal/stores/STORE_A/auto-reply", json={"enabled": True}).status_code == 200
    return client


def _fit_clarify(**kw):
    claims = [{"text": "Please send us your VIN so we can check the fitment.", "evidence_ids": ["kb:KB-FIT@1"]}]
    return draft_json(intent="fitment", intents=["fitment"], action="clarify", claims=claims,
                      reply_text="Hi, please send us your VIN so we can check the fitment for you.",
                      reply_zh="您好，请发一下车架号，我们帮您核对适配。", **kw)


@pytest.mark.parametrize("conf, auto", [("high", True), (None, True), ("low", False)])
def test_low_confidence_never_auto_sent(auto_client, conf, auto):
    use_fake_llm(_fit_clarify(intent_confidence=conf))
    sent = send_buyer(auto_client, "Does it fit my car?", buyer=f"buyer_conf_{conf}")
    from app.worker import worker

    worker.run_once()
    d = detail(auto_client, sent["conversation_id"])
    if auto:  # 对照：同样的草稿把握度高 / 没给把握度会自动发
        assert d["job"]["state"] == "SENT" and d["messages"][-1]["role"] == "SELLER"
    else:
        assert d["job"]["state"] == "REVIEW_REQUIRED" and "INTENT_UNCERTAIN" in d["job"]["decision_reasons"]
        assert d["messages"][-1]["role"] == "BUYER"
        from app.db import session_scope
        from app.models import OutboundMessage

        with session_scope() as db:
            assert db.scalars(select(OutboundMessage).where(OutboundMessage.conversation_id == sent["conversation_id"])).all() == []


# ---------------------------------------------------------------- 3.5 新字段缺失 / 乱写
def test_missing_new_fields(client, delivered):
    raw = json.loads(draft_json(claims=[]))
    for k in ("intent_confidence", "intent_basis", "inherits_issue", "state_update"):
        raw.pop(k, None)
    use_fake_llm(json.dumps(raw, ensure_ascii=False))
    sent = send_buyer(client, "Hello, quick question about my order.", buyer="buyer_missing")
    from app.worker import worker

    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["agent_draft"] is not None and "OUTPUT_INVALID" not in (d["job"]["decision_reasons"] or [])
    assert d["agent_draft"]["intent_confidence"] is None and d["agent_draft"]["intent_basis"] == []
    assert d["agent_draft"]["inherits_issue"] is None
    assert isinstance(d["state_card"], dict)


def test_garbage_inherits_and_basis_dropped(client, delivered):
    cid, m1 = _not_received_then_ticket(client)
    _, m2, job, d = run_round(client, "Danke für die Info.", intent="thanks_closing", intents=["thanks_closing"],
                              action="answer", intent_confidence="medium",
                              inherits_issue="sys:ticket:DOES-NOT-EXIST", intent_basis=["SELF", "msg-999", m1, "SELF"])
    assert d.inherits_issue is None and d.intent_basis == [m2, m1]  # 不在状态卡 / 不在上下文的去掉，重复去重
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job.decision_reasons  # 乱写的沿用问题不会带出“没收到”

    for v in ("null", "None", "  "):
        from app.agent.schemas import ReplyDraftOutput

        raw = json.loads(draft_json(inherits_issue=v))
        assert ReplyDraftOutput.model_validate(raw).inherits_issue is None


def test_garbage_confidence_value_normalized_without_repair(client, delivered):
    """把握度乱写（不在 high/medium/low 里）：程序直接归一（认不出当 null），整份输出照常用，不触发修复、不多花 token。
    （2026-11-09 按产品要求从“走修复”改为“直接归一”）"""
    from app.agent.schemas import ReplyDraftOutput
    from app.worker import worker

    use_fake_llm(draft_json(intent_confidence="very sure"))  # 只给一份输出：要是走了修复就没有第二份可用
    sent = send_buyer(client, "Hello there", buyer="buyer_bad_conf1")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["agent_draft"] is not None and d["agent_draft"]["intent_confidence"] is None
    assert "OUTPUT_INVALID" not in d["job"]["decision_reasons"]
    assert not any(s["node"] == "repair_output" for s in d["steps"])
    for raw, want in (("HIGH", "high"), (" Medium ", "medium"), ("低", "low"), ("certain", None), (None, None)):
        assert ReplyDraftOutput.model_validate(json.loads(draft_json(intent_confidence=raw))).intent_confidence == want


# ---------------------------------------------------------------- 3.6 再要发票接沿用的发票问题
def test_invoice_follow_up_uses_inherited_issue(monkeypatch):
    from types import SimpleNamespace

    from app.agent import nodes

    state = {"snapshot": {"buyer_texts": ["Any news?"]}, "observations": [],
             "memory": {"messages": [{"id": "a", "text": "Please send an invoice to rita@example.com", "in_task": False,
                                      "role": "BUYER"}]}}
    deps = SimpleNamespace(settings=config.get_settings())
    monkeypatch.setattr(nodes, "_fresh_buyer_texts", lambda s, d: ["Any news?"])
    monkeypatch.setattr(nodes, "_waiting_order_choice", lambda s, d: False)
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:invoice", "source": "system", "flags": ["INVOICE"]},
                       {"id": "sys:ticket:X", "source": "system", "flags": ["LOGISTICS", "NOT_RECEIVED"]}]}
    carried = cs.carried_flags(card, "sys:invoice", "Any news?")
    assert carried == {"INVOICE"}
    assert nodes._asks_invoice_again(state, deps, ["invoice_vat"], carried) is True
    assert nodes._asks_invoice_again(state, deps, ["thanks_closing", "invoice_vat"], carried) is False
    assert nodes._asks_invoice_again(state, deps, ["invoice_vat"], cs.carried_flags(card, "sys:ticket:X", "Any news?")) is False
    assert nodes._remembered_invoice_email(state) == "rita@example.com"


# ---------------------------------------------------------------- 3.7 旧草稿 / 旧会话没有新字段
def test_old_rows_without_new_fields(client, delivered):
    from app.db import session_scope
    from app.models import Conversation, Draft

    cid, _, job, d = run_round(client, "Where is my parcel?", intent="shipping_status", intents=["shipping_status"],
                               intent_confidence="high", intent_basis=["SELF"], inherits_issue=None)
    with session_scope() as db:
        dr = db.get(Draft, d.id)
        dr.intent_confidence = dr.intent_basis = dr.inherits_issue = None
        conv = db.get(Conversation, cid)
        conv.state_card = conv.state_updated_at = None
    r = client.get(f"/internal/conversations/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_draft"]["intent_confidence"] is None and body["agent_draft"]["intent_basis"] == []
    assert body["agent_draft"]["inherits_issue"] is None and body["state_card"] is None
    t = client.get(f"/internal/jobs/{job.id}/trace")
    assert t.status_code == 200 and t.json()["state_card"] is None
    assert client.get("/internal/conversations").status_code == 200

    # 旧会话（没有状态卡）再来一条：照常起草，状态卡从空建起
    seller_reply(cid, "It is on the way.")
    _, _, job2, d2 = run_round(client, "Any news?", intent="shipping_status", intents=["shipping_status"])
    assert d2 is not None and isinstance(card_of(cid), dict)


# ---------------------------------------------------------------- 3.8 schema_instruction 改动不影响识图解析
def test_schema_instruction_keeps_constraints():
    from app.agent.schemas import ReplyDraftOutput
    from app.logistics.tickets import TicketReply
    from app.feishu.cases import CaseReply
    from app.providers.chat import _no_titles, schema_instruction
    from app.providers.vision import ImageObservationOutput

    for schema in (ReplyDraftOutput, ImageObservationOutput, TicketReply, CaseReply):
        full = schema.model_json_schema()
        slim = json.loads(schema_instruction(schema).split("\n", 1)[1])
        assert slim == _no_titles(full)

        def walk(a, b):  # 除了字符串 title，结构、required、enum、properties 名都没变
            if isinstance(a, dict):
                assert set(b) == {k for k, v in a.items() if not (k == "title" and isinstance(v, str))}
                for k in b:
                    walk(a[k], b[k])
            elif isinstance(a, list):
                assert len(a) == len(b)
                for x, y in zip(a, b):
                    walk(x, y)
            else:
                assert a == b

        walk(full, slim)
        assert '"title"' not in json.dumps(slim) or "title" in json.dumps(full.get("properties", {}))
    assert len(schema_instruction(ReplyDraftOutput)) < len(json.dumps(ReplyDraftOutput.model_json_schema(), ensure_ascii=False))


def test_vision_parse_after_schema_change(app_env):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.providers.vision import VisionModelProvider

    obs = {"image_kind": "故障码或仪表屏幕", "quality": "清晰", "description": "诊断仪屏幕显示故障码",
           "visible_text": ["C1234 Wheel speed sensor"], "part_numbers": [], "error_codes": ["C1234"], "vin_masked": "",
           "damage_observed": False, "damage_description": "", "uncertain": [], "contains_personal_info": False}
    seen = []

    class Spy(GenericFakeChatModel):
        def invoke(self, messages, *a, **k):
            seen.append(messages)
            return super().invoke(messages, *a, **k)

    for content in (json.dumps(obs, ensure_ascii=False), "```json\n" + json.dumps(obs, ensure_ascii=False) + "\n```"):
        p = VisionModelProvider(config.get_settings(), model=Spy(messages=iter([AIMessage(content=content)])))
        r = p.observe(b"\xff\xd8fake", "image/jpeg", "Wheel speed sensor")
        assert r.parsed is not None and r.parsed.error_codes == ["C1234"] and r.parsed.image_kind == "故障码或仪表屏幕"
    assert "JSON Schema" in seen[0][0].content and '"title"' not in seen[0][0].content


# ---------------------------------------------------------------- 3.3 长度上限：裁剪顺序
def test_trim_order_exact(monkeypatch):
    from app import conversation_state as cs

    def iss(i, src):
        return {"id": f"{'sys:ticket' if src == 'system' else 'm:other'}:{i}", "source": src, "intent": "other",
                "summary_zh": f"问题{i}" * 6, "order_id": f"0{i}-12345-67890", "since": "2026-11-01", "status": "处理中",
                "refs": [], "flags": []}

    card = {"topic": "主题", "issues": [iss(1, "system"), iss(2, "system"), iss(3, "model"), iss(4, "model")],
            "resolved": [{"summary_zh": f"已解决{i}" * 5, "intent": None, "at": "2026-11-01"} for i in range(4)],
            "facts": {}, "promises": [f"答应{i}" * 8 for i in range(4)]}
    order = []
    snap = json.loads(json.dumps(card))
    while True:
        monkeypatch.setattr(cs, "MAX_TOKENS", cs.tokens(snap) - 1)
        nxt = cs._trim(json.loads(json.dumps(snap)))
        if nxt == snap:
            break
        if len(nxt["resolved"]) < len(snap["resolved"]):
            order.append(("resolved", snap["resolved"][0]["summary_zh"][:4]))
        elif len(nxt["promises"]) < len(snap["promises"]):
            order.append(("promise", snap["promises"][-1][:3]))
        else:
            gone = [i["id"] for i in snap["issues"] if i not in nxt["issues"]]
            order.append(("issue", gone[0]))
        snap = nxt
    assert order == [("resolved", "已解决0"), ("resolved", "已解决1"), ("resolved", "已解决2"), ("resolved", "已解决3"),
                     ("promise", "答应3"), ("promise", "答应2"), ("promise", "答应1"),
                     ("issue", "m:other:4"), ("issue", "m:other:3"),
                     ("issue", "sys:ticket:2")]  # 实际：最后还会砍系统问题，只剩 1 条
    assert [i["id"] for i in snap["issues"]] == ["sys:ticket:1"] and snap["promises"] == ["答应0" * 8]


# ---------------------------------------------------------------- eBay 实时查到的“未收到货咨询”
def test_live_inr_case_flags_not_received(app_env):
    from app import conversation_state as cs
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "INR-T", "buyer_inr", "110000000001", "EBAY_US", "listing")
        obs = [{"tool_name": "get_case_status", "status": "FOUND",
                "data": [{"case_type": "INR", "case_id": "5300000001", "status": "OPEN", "order_id": "01-1",
                          "opened_at": "2026-11-08T10:00:00Z", "last_action": "OPEN/WAITING_SELLER"}]}]
        issue = next(i for i in cs.system_issues(db, conv, obs) if i["id"].startswith("sys:case:"))
        assert issue["intent"] == "delivery_delay" and "NOT_RECEIVED" in issue["flags"]
