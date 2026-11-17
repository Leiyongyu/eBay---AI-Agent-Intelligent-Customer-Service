"""翻译单独用一个模型：LLM_TRANSLATE_MODEL（留空用回复模型），默认关思考；回复模型的思考设置不受影响；用量记实际的翻译模型。"""
from app.config import Settings


def _settings(**kw):
    base = dict(llm_provider="openai_compatible", llm_chat_model="qwen3.7-max-preview", llm_api_key="k",
                llm_base_url="https://example.invalid/v1", llm_extra_body='{"enable_thinking": true}')
    return Settings(**{**base, **kw})


def test_translator_uses_own_model_without_thinking():
    from app.providers.chat import ChatModelProvider
    from app.usage import chat_model, translate_model

    s = _settings(llm_translate_model="qwen3.6-plus")
    p = ChatModelProvider(s)
    assert p.model.model_name == "qwen3.7-max-preview" and p.model.extra_body == {"enable_thinking": True}
    assert p.translator.model_name == "qwen3.6-plus" and p.translator.extra_body == {"enable_thinking": False}
    assert translate_model(s) == ("openai_compatible", "qwen3.6-plus") and chat_model(s)[1] == "qwen3.7-max-preview"

    # 留空：沿用回复模型，但翻译仍关思考
    s2 = _settings()
    p2 = ChatModelProvider(s2)
    assert p2.translator.model_name == "qwen3.7-max-preview" and p2.translator.extra_body == {"enable_thinking": False}
    # 要开思考也可以
    s3 = _settings(llm_translate_model="qwen3.6-plus", llm_translate_thinking=True)
    assert ChatModelProvider(s3).translator.extra_body == {"enable_thinking": True}


def test_injected_fake_model_used_for_translation():
    """测试注入假模型时，翻译也用它（不另外连服务商）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.providers.chat import ChatModelProvider

    fake = GenericFakeChatModel(messages=iter([AIMessage(content="你好")]))
    p = ChatModelProvider(_settings(llm_translate_model="qwen3.6-plus"), model=fake)
    assert p.translate("Hallo") == "你好"
