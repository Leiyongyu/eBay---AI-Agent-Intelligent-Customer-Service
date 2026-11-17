"""验收（测试工程师，2026-11-09 第三轮）：问题 E 修复后的“短追问接标记”反例——
新订单的物流问题、意图 other 的寒暄句、一条消息两件事（物流 + 适配）、INR 未收到货咨询被判成退货。"""
import pytest

from test_conversation_state import ORDER, card_of, delivered, run_round, seller_reply  # noqa: F401  delivered 是夹具

OTHER_ORDER = "26-99999-US"  # 另一笔订单（之前说没收到的那单）


def _ticket(cid, order, type_name="妥投未收到", code="CJCARRY1"):
    from app.db import session_scope, utcnow
    from app.models import LogisticsTicket

    with session_scope() as db:
        db.add(LogisticsTicket(io_code=code, conversation_id=cid, store_id="STORE_A", reference_no=order,
                               type_name=type_name, io_status=3, submitted_at=utcnow(), added_at="2026-11-09 10:00:00"))


def _start(client, ticket_order):
    """第 1 轮买家问别的（不说没收到），同事回复；再给会话挂一张某订单的妥投未收到查件单。"""
    cid, m1, _, _ = run_round(client, "Hello, I have a question.", intent="other", intents=["other"], action="clarify",
                              intent_confidence="medium")
    seller_reply(cid, "Sure, how can we help?")
    _ticket(cid, ticket_order)
    return cid, m1


# ---------------------------------------------------------------- 反例 1：查件单是另一笔订单，买家问这笔订单的物流
def test_follow_up_about_other_order_not_marked_not_received(client, delivered):
    cid, _ = _start(client, OTHER_ORDER)
    _, _, job, d = run_round(client, "What about this order? Any news?", intent="shipping_status",
                             intents=["shipping_status"], action="answer", intent_confidence="medium", inherits_issue=None)
    assert delivered == [ORDER] or ORDER in delivered
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job.decision_reasons


def test_follow_up_same_order_still_marked(client, delivered):
    """对照：查件单就是这笔订单，买家短追问、模型判物流意图、没给沿用 → 照样接上。"""
    cid, _ = _start(client, ORDER)
    _, _, job, _ = run_round(client, "Any news?", intent="shipping_status", intents=["shipping_status"], action="answer",
                             intent_confidence="medium", inherits_issue=None)
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons


# ---------------------------------------------------------------- 反例 2：意图 other 的寒暄句里带“还没 / noch nicht”
@pytest.mark.parametrize("text", ["好的谢谢，我还没到家，晚点再说", "Ok, ich bin noch nicht zu Hause, melde mich später."])
def test_other_intent_small_talk_carries_everything(client, delivered, text):
    """按设计：意图只有 other 时接全部未解决标记。这里记录实际行为（会判妥投未收到），不算错，供产品判断。"""
    cid, _ = _start(client, ORDER)
    _, _, job, _ = run_round(client, text, intent="other", intents=["other"], action="answer", intent_confidence="medium")
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons


def test_thanks_with_still_not_does_not_carry(client, delivered):
    """寒暄被判成 thanks_closing（不是 other）：不接。"""
    cid, _ = _start(client, ORDER)
    _, _, job, _ = run_round(client, "Thanks, still waiting but no problem.", intent="thanks_closing",
                             intents=["thanks_closing"], action="answer", intent_confidence="high")
    assert "TRACKING_DELIVERED_NOT_RECEIVED" not in job.decision_reasons


# ---------------------------------------------------------------- 反例 3：一条消息两件事（物流 + 适配）
def test_two_things_short_carries_logistics(client, delivered):
    cid, _ = _start(client, ORDER)
    _, _, job, d = run_round(client, "Any news on the parcel? And does it fit a Golf 7?", intent="delivery_delay",
                             intents=["delivery_delay", "fitment"], action="answer", intent_confidence="high")
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons and d.intents == ["delivery_delay", "fitment"]


def test_two_things_fitment_first_still_carries(client, delivered):
    """主意图是适配、物流排第二：也要接上（看全部意图，不只看主意图）。"""
    cid, _ = _start(client, ORDER)
    _, _, job, _ = run_round(client, "Does it fit a Golf 7? And any news on the parcel?", intent="fitment",
                             intents=["fitment", "delivery_delay"], action="answer", intent_confidence="high")
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons


def test_two_things_long_message_misses_carry(client, delivered):
    cid, _ = _start(client, ORDER)
    text = ("Hello again, just checking in: any news on the parcel? Also, while I'm writing anyway, could you please tell me "
            "whether the same sensor also fits my second car, a 2014 Skoda Octavia III 1.6 TDI with the CLHA engine?")
    assert len(text) > 160
    _, _, job, _ = run_round(client, text, intent="delivery_delay", intents=["delivery_delay", "fitment"], action="answer",
                             intent_confidence="high")
    assert "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons


# ---------------------------------------------------------------- 反例 4：INR 未收到货咨询，模型判成 return_refund
def test_inr_case_follow_up_judged_return_refund():
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:case:INQUIRY:5300000001", "source": "system", "intent": "delivery_delay",
                        "flags": ["LOGISTICS", "NOT_RECEIVED"]}]}
    assert cs.carried_flags(card, None, "I opened a case. Any news?", ["return_refund"]) >= {"NOT_RECEIVED"}


def test_carry_unit_matrix():
    """单元级：各意图接哪些标记。"""
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:ticket:A", "source": "system", "intent": "delivery_delay", "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "sys:invoice", "source": "system", "intent": "invoice_vat", "flags": ["INVOICE"]},
                       {"id": "m:damaged_wrong:", "source": "model", "intent": "damaged_wrong", "flags": ["RESHIP"]}]}
    t = "Any news?"
    assert cs.carried_flags(card, None, t, None) == {"LOGISTICS", "NOT_RECEIVED", "INVOICE", "RESHIP"}  # 没起草：全接
    assert cs.carried_flags(card, None, t, ["other"]) == {"LOGISTICS", "NOT_RECEIVED", "INVOICE", "RESHIP"}
    assert cs.carried_flags(card, None, t, ["shipping_status"]) == {"LOGISTICS", "NOT_RECEIVED", "RESHIP"}
    assert cs.carried_flags(card, None, t, ["invoice_vat"]) == {"INVOICE"}
    assert cs.carried_flags(card, None, t, ["damaged_wrong"]) == {"RESHIP"}
    assert cs.carried_flags(card, None, t, ["fitment"]) == set()
    assert cs.carried_flags(card, "sys:invoice", "What about the Golf 7 fitment?", ["fitment"]) == {"INVOICE"}  # 明说沿用以它为准
    assert cs.carried_flags(card, None, "Does it fit a Golf 7?", None) == set()  # 不是追问句：不接


# ---------------------------------------------------------------- G：INR 端到端（实时 case → 下一轮短追问）
def test_inr_observation_then_follow_up(client, delivered, monkeypatch):
    from dataclasses import replace

    from app.tools import registry

    def fake_case(ctx, src, args, db=None):
        return "FOUND", [{"case_type": "INR", "case_id": "5300000009", "order_id": args.order_id, "status": "OPEN",
                          "opened_at": "2026-11-08T10:00:00Z", "last_action": "OPEN/WAITING_SELLER"}], \
            f"case:{args.order_id}", None

    monkeypatch.setitem(registry.TOOLS, "get_case_status", replace(registry.TOOLS["get_case_status"], fn=fake_case))
    cid, _, _, _ = run_round(client, "Hello, I have a question.", intent="other", intents=["other"], action="clarify")
    issue = next(i for i in card_of(cid)["issues"] if i["id"].startswith("sys:case:"))
    assert issue["id"] == "sys:case:INQUIRY:5300000009" and "NOT_RECEIVED" in issue["flags"]
    seller_reply(cid, "Sorry, we are checking.")
    delivered.clear()
    _, _, job, _ = run_round(client, "Any news?", intent="delivery_delay", intents=["delivery_delay"], action="answer")
    assert delivered and "TRACKING_DELIVERED_NOT_RECEIVED" in job.decision_reasons


# ---------------------------------------------------------------- 第五轮：E2 / G2 / E3 修复后的反例（2026-11-10）
INR_CARD = {"issues": [{"id": "sys:case:INQUIRY:5300000001", "source": "system", "intent": "delivery_delay",
                        "order_id": "110000000003-2345678901001", "flags": ["LOGISTICS", "NOT_RECEIVED"]}]}


def test_inr_case_legacy_order_id_still_carried():
    from app import conversation_state as cs

    got = cs.carried_flags(INR_CARD, None, "Any news?", ["delivery_delay"], orders=["26-10002-US"])
    assert "NOT_RECEIVED" in got


def test_order_id_whitespace_or_case():
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:ticket:CJ1", "source": "system", "intent": "delivery_delay", "order_id": " 26-10002-us ",
                        "flags": ["LOGISTICS", "NOT_RECEIVED"]}]}
    assert "NOT_RECEIVED" in cs.carried_flags(card, None, "Any news?", ["delivery_delay"], orders=["26-10002-US"])


def test_order_scope_record_current_behavior():
    """记录现状：本轮订单不唯一（两单都在问）或查不到订单（[]）时，只接没写订单的问题；带订单的查件单接不上。"""
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:ticket:CJ1", "source": "system", "intent": "delivery_delay", "order_id": "26-10002-US",
                        "flags": ["LOGISTICS", "NOT_RECEIVED"]},
                       {"id": "m:delivery_delay:", "source": "model", "intent": "delivery_delay", "order_id": None, "flags": ["RESHIP"]}]}
    t, i = "Any news?", ["delivery_delay"]
    assert cs.carried_flags(card, None, t, i, orders=["26-10002-US"]) == {"LOGISTICS", "NOT_RECEIVED", "RESHIP"}
    assert cs.carried_flags(card, None, t, i, orders=["26-10002-US", "26-10009-US"]) == {"RESHIP"}  # 两单都在问
    assert cs.carried_flags(card, None, t, i, orders=[]) == {"RESHIP"}  # 查不到订单
    assert cs.carried_flags(card, None, t, i, orders=None) == {"LOGISTICS", "NOT_RECEIVED", "RESHIP"}
    assert cs.carried_flags(card, "sys:ticket:CJ1", t, i, orders=["26-10009-US"]) == {"LOGISTICS", "NOT_RECEIVED"}  # 明说沿用不受限


def test_observed_case_disappears_when_closed(client, delivered, monkeypatch):
    """上一轮实时查到的 INR，这一轮实时查到已结：状态卡里消失。"""
    from dataclasses import replace

    from app.tools import registry

    status = {"v": "OPEN"}

    def fake_case(ctx, src, args, db=None):
        return "FOUND", [{"case_type": "INR", "case_id": "5300000019", "order_id": args.order_id, "status": status["v"],
                          "opened_at": "2026-11-08T10:00:00Z", "last_action": "x"}], f"case:{args.order_id}", None

    monkeypatch.setitem(registry.TOOLS, "get_case_status", replace(registry.TOOLS["get_case_status"], fn=fake_case))
    cid, _, _, _ = run_round(client, "Hello, I have a question.", intent="other", intents=["other"], action="clarify")
    assert any(i["id"].endswith("5300000019") for i in card_of(cid)["issues"])
    seller_reply(cid, "Sure.")
    status["v"] = "CLOSED"
    run_round(client, "Any news?", intent="delivery_delay", intents=["delivery_delay"], action="answer")
    assert not any(i["id"].endswith("5300000019") for i in card_of(cid)["issues"])


@pytest.mark.parametrize("text, want", [
    ("Any news?", True),
    ("Hello, I ordered the sensor for my Golf 7 1.6 TDI last week and wanted to ask something else first. Any news on the parcel?", True),
    ("Nr. 5 ist noch nicht da", True),
    ("Passt das Teil auf einen VW Golf 7 Variant 1.6 TDI, Baujahr 2015, Motorcode CLHA, Getriebe DSG, Ausstattung Highline?", False),
    ("Link: https://www.ebay.de/itm/123456?hash=item1c still waiting for an answer about compatibility with my car please", False),  # 网址里的 ? 不断句：整句超过 100 字
])
def test_is_follow_up_sentences(text, want):
    from app import conversation_state as cs

    assert cs.is_follow_up(text) is want


@pytest.mark.parametrize("text", [
    "Ich habe zu meinem neuen Golf 7 Variant aus der Werkstatt mehrere Fragen, z.B. ob ich das Thermostat noch nicht einbauen soll.",
    "Für meinen Golf 7 Variant Baujahr 2015 habe ich in der Garage ca. drei Teile liegen, die ich noch nicht eingebaut habe, passen die?",
    "Wir haben in der Firma bzw. in der Werkstatt mehrere Fahrzeuge, die ich noch nicht umgerüstet habe – passt das Teil auf alle?",
], ids=["z_b", "ca", "bzw"])
def test_is_follow_up_german_abbreviations(text):
    from app import conversation_state as cs

    assert len(text) > 100
    assert cs.is_follow_up(text) is False


# ---------------------------------------------------------------- 第六轮（2026-11-10）：E3b 修复后的反例
@pytest.mark.parametrize("text", [
    "Ich habe zu meinem neuen Golf 7 Variant aus der Werkstatt mehrere Fragen, z. B. ob ich das Thermostat noch nicht einbauen soll.",
    "Für meinen Golf 7 Variant aus der Werkstatt habe ich mehrere Teile, d. h. die Sensoren, die ich noch nicht eingebaut habe, passen?",
], ids=["z_B_spaced", "d_h_spaced"])
def test_is_follow_up_spaced_abbreviations(text):
    from app import conversation_state as cs

    assert len(text) > 100 and cs.is_follow_up(text) is False


def test_unconvertible_legacy_order_treated_as_no_order():
    """换算不了的旧格式订单号按“没写订单”处理：任何本轮订单都接（正式库 14 张换算不了，全是已结单、对接店铺 0 张，不进状态卡）。"""
    from app import conversation_state as cs

    card = {"issues": [{"id": "sys:case:INQUIRY:1", "source": "system", "intent": "delivery_delay",
                        "order_id": "110000000003-2345678901001", "flags": ["LOGISTICS", "NOT_RECEIVED"]}]}
    assert "NOT_RECEIVED" in cs.carried_flags(card, None, "Any news?", ["delivery_delay"], orders=["26-10002-US"])
    card["issues"][0]["order_key"] = "26-99999-US"  # 换算成功、是另一单：不接
    assert cs.carried_flags(card, None, "Any news?", ["delivery_delay"], orders=["26-10002-US"]) == set()
    card["issues"][0]["order_key"] = "26-10002-US"
    assert "NOT_RECEIVED" in cs.carried_flags(card, None, "Any news?", ["delivery_delay"], orders=[" 26-10002-us "])
