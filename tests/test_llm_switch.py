"""大模型总开关 LLM_ENABLED=false：回复、识图、向量、自动翻译全部停用；买家消息照常进来、进待审核由人工回复。"""
from app.config import get_settings
from app.worker import worker
from test_pipeline import detail, send_buyer


def test_llm_switch_off(client, monkeypatch):
    from app.providers.embeddings import embedding_ready
    from app.providers.vision import get_vision_provider
    from app.translation import translator

    s = get_settings()
    for k, v in {"llm_enabled": False, "llm_provider": "openai_compatible", "llm_chat_model": "m", "llm_api_key": "k",
                 "auto_translate_buyer_messages": True,
                 "vision_enabled": True}.items():
        monkeypatch.setattr(s, k, v, raising=False)
    assert s.agent_not_ready_reasons()[0].startswith("LLM_ENABLED=false")
    assert get_vision_provider() is None and embedding_ready()[0] is False and translator.enabled is False

    sent = send_buyer(client, "Where is my order?")
    assert worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "AGENT_NOT_READY" in d["job"]["decision_reasons"]
    assert d["drafts"] == []
    assert {t["tool_name"] for t in d["tool_calls"]} >= {"get_authorized_order_context", "get_package_status"}  # 数据照常查
