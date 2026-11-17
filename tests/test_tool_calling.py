"""原生工具调用：模型自己决定查什么；不能查别的买家的订单；未知工具与轮数上限；不支持工具的模型退回单次生成。"""
from test_pipeline import detail, draft_json, send_buyer, tool_call, use_fake_llm, use_fake_tool_llm


def run(client, text, buyer="buyer_us_001"):
    from app.worker import worker

    sent = send_buyer(client, text, buyer=buyer)
    worker.run_once()
    return detail(client, sent["conversation_id"])


def test_cannot_query_other_buyers_order(client):
    # 店铺 STORE_A 的订单 26-10001-US 属于 buyer_us_001；换个买家来问同一单，物流和售后都查不到
    use_fake_tool_llm(tool_call("get_package_status", {"order_id": "26-10001-US"}), draft_json(claims=[]))
    d = run(client, "Track order 26-10001-US", buyer="someone_else")
    pkg = next(t for t in d["tool_calls"] if t["tool_name"] == "get_package_status")
    assert pkg["status"] == "NOT_FOUND" and pkg["message"] == "订单不属于当前买家"


def test_unknown_tool_and_round_limit(client, monkeypatch):
    from app import config

    monkeypatch.setenv("AGENT_MAX_TOOL_ROUNDS", "2")
    config.get_settings.cache_clear()
    use_fake_tool_llm(tool_call("delete_everything"), tool_call("get_authorized_order_context", call_id="c2"),
                      draft_json())  # 第 2 轮后达到上限，带着已查到的结果直接作答
    d = run(client, "Where is my order?")
    compose = next(s for s in d["steps"] if s["node"] == "compose_reply")
    assert compose["detail"]["tool_rounds"] == 2
    assert [c["tool"] for c in compose["detail"]["tool_calls"]] == ["get_authorized_order_context"]  # 未知工具不执行
    assert d["drafts"] and d["drafts"][0]["text"]


def test_model_without_tool_support_falls_back(client):
    use_fake_llm(draft_json(claims=[]))
    d = run(client, "Hello")
    compose = next(s for s in d["steps"] if s["node"] == "compose_reply")
    assert compose["detail"]["tool_rounds"] == 0 and d["drafts"]
