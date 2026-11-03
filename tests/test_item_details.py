"""当前商品详情：模型按需请求 get_item_details；兼容车型按买家车型筛选；商品参数类问题可按商品信息自动发；会话商品卡片。"""
import json

import pytest

from app import config
from app.agent.schemas import RuntimeContext
from app.guards import risk
from app.sources.ebay import description_text, item_url
from app.tools.readonly import ItemDetailsArgs, get_item_details
from test_auto_reply import gate
from test_pipeline import detail, draft_json, send_buyer, tool_call, use_fake_llm, use_fake_tool_llm

CTX = RuntimeContext(store_id="S", conversation_id=1, buyer_key="b", marketplace_id="EBAY_US", item_id="397942959345",
                     job_id=1, run_generation=1)
COMPAT = [{"Year": str(y), "Make": "Chevrolet", "Model": m, "Trim": "LT"} for y in range(2007, 2014)
          for m in ("Silverado 1500", "Tahoe")] + [{"Year": "2008", "Make": "GMC", "Model": "Acadia"}]


class FakeSource:
    name = "fake"

    def __init__(self, listing):
        self.listing = listing

    def get_listing(self, store_id, item_id):
        return self.listing


def listing(**kw):
    base = {"item_id": "397942959345", "marketplace_id": "EBAY_US", "title": "Window Switch", "sku": "GM-1",
            "variations": [{"sku": "GM-1", "aspects": {}, "quantity_available": 2}],
            "item_specifics": {"Brand": "Jumach"}, "compatibility": COMPAT, "compatibility_total": len(COMPAT),
            "excluded_ship_to": [f"X{i}" for i in range(25)], "description_text": "Package: 1 x switch"}
    base.update(kw)
    return base


def test_item_details_filters_compatibility_and_hides_quantity():
    status, data, ref, _ = get_item_details(CTX, FakeSource(listing()), ItemDetailsArgs(vehicle="2010 chevrolet tahoe"))
    assert (status, ref) == ("FOUND", "listing:397942959345")
    assert data["compatibility_match_count"] == 1 and data["compatibility_matches"][0]["Model"] == "Tahoe"
    assert data["variations"] == [{"sku": "GM-1", "aspects": {}, "in_stock": True}]  # 只说有没有货
    assert "quantity_available" not in json.dumps(data)
    assert len(data["excluded_ship_to"]) == 21 and data["excluded_ship_to"][-1] == "…共 25 个"

    _, data, _, _ = get_item_details(CTX, FakeSource(listing()), ItemDetailsArgs())
    assert data["compatibility_summary"] == ["Chevrolet Silverado 1500 2007-2013", "Chevrolet Tahoe 2007-2013",
                                             "GMC Acadia 2008"]
    _, data, _, _ = get_item_details(CTX, FakeSource(listing()), ItemDetailsArgs(vehicle="2015 Ford F-150"))
    assert data["compatibility_match_count"] == 0
    assert get_item_details(CTX, FakeSource(None), ItemDetailsArgs())[0] == "NOT_FOUND"


def test_description_text_and_item_url():
    raw = ("<style>body{font:x}</style><script>alert(1)</script><!-- c --><div>Package Including:&nbsp;1 x Switch</div>"
           "<p>Fits Tahoe</p>")
    assert description_text(raw) == "Package Including: 1 x Switch Fits Tahoe"
    assert description_text("<p>" + "a" * 2000 + "</p>", limit=10) == "a" * 10 + "…"
    assert item_url("1", "EBAY_GB") == "https://www.ebay.co.uk/itm/1"
    assert item_url("1", "UNKNOWN") == "https://www.ebay.com/itm/1"


def test_gate_listing_grounded_item_questions():
    kw = dict(kb_evidence=0, listing_evidence=1, auto_listing_intents={"item_spec", "shipping_cost"})
    assert gate(intents=["shipping_cost"], **kw).decision == "AUTO_ELIGIBLE"  # L1 运费问题按商品信息作答
    assert gate(intents=["item_spec"], auto_send_intents=set(), **kw).decision == "AUTO_ELIGIBLE"
    fit = gate(intents=["fitment"], buyer_text="Passt das?", **kw)  # 适配结论仍要人工
    assert {"RISK_NOT_L0", "INTENT_NOT_WHITELISTED", "NO_KB_EVIDENCE"} <= set(fit.reasons)
    no_ref = gate(intents=["shipping_cost"], kb_evidence=0, listing_evidence=0,
                  auto_listing_intents={"shipping_cost"})
    assert "NO_KB_EVIDENCE" in no_ref.reasons


@pytest.fixture()
def listing_env(client, monkeypatch):
    monkeypatch.setenv("AUTO_SEND_ENABLED", "true")
    monkeypatch.setenv("AUTO_SEND_INTENTS", "item_spec")
    monkeypatch.setenv("AUTO_SEND_LISTING_INTENTS", "item_spec,shipping_cost")
    config.get_settings.cache_clear()
    client.post("/internal/stores/STORE_A/auto-reply", json={"enabled": True})
    return client


def item_draft(**kw):
    return draft_json(intent="item_spec", intents=["item_spec"],
                      claims=[{"text": "The pads are ceramic.", "evidence_ids": ["listing:110000000001"]}],
                      reply_text="Hi, these brake pads are ceramic.", reply_zh="您好，这款刹车片是陶瓷材质。", **kw)


def test_model_calls_item_tool_then_auto_sends(listing_env):
    from app.worker import worker

    client = listing_env
    use_fake_tool_llm(tool_call("get_item_details"), item_draft())
    sent = send_buyer(client, "What material are these pads?", buyer="buyer_item_1")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    calls = [(t["tool_name"], t["status"]) for t in d["tool_calls"]]
    assert calls.count(("get_item_details", "FOUND")) == 1
    compose = next(s for s in d["steps"] if s["node"] == "compose_reply")
    assert compose["detail"]["tool_calls"] == [{"tool": "get_item_details", "args": {}, "status": "FOUND"}]
    assert d["job"]["state"] == "SENT" and d["messages"][-1]["trace"]["auto"] is True
    assert d["conversation"]["item_url"] == "https://www.ebay.com/itm/110000000001"

    card = client.get(f"/internal/conversations/{sent['conversation_id']}/listing").json()
    assert card["title"].startswith("StarDrive Front Ceramic Brake Pads") and card["sku"] == "SD-BP-1023"
    assert card["url"] == "https://www.ebay.com/itm/110000000001" and card["error"] is None


def test_no_item_lookup_when_model_does_not_call_tool(listing_env):
    from app.worker import worker

    use_fake_llm(draft_json(intent="thanks_closing", intents=["thanks_closing"], claims=[],
                            reply_text="You're welcome!", reply_zh="不客气！"))
    sent = send_buyer(listing_env, "Thanks!", buyer="buyer_item_2")
    worker.run_once()
    d = detail(listing_env, sent["conversation_id"])
    assert all(t["tool_name"] != "get_item_details" for t in d["tool_calls"])
