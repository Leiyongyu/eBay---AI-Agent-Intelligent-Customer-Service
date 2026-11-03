import os

import pytest

from app import config


def test_database_password_is_url_encoded(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/ebay_cs_agent")
    monkeypatch.setenv("DATABASE_PASSWORD", "p@ss:w/rd#1")
    config.get_settings.cache_clear()
    s = config.get_settings()
    from sqlalchemy.engine import make_url

    assert make_url(s.effective_database_url).password == "p@ss:w/rd#1"
    assert "p@ss" not in s.database_display
    config.get_settings.cache_clear()


def test_workspace_placeholder_blocks_readiness(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_CHAT_MODEL", "qwen3.7-plus")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"enable_thinking": false}')
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.extra_body() == {"enable_thinking": False}
    assert any("占位符" in r for r in s.agent_not_ready_reasons())
    monkeypatch.setenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    config.get_settings.cache_clear()
    assert config.get_settings().agent_not_ready_reasons() == []
    config.get_settings.cache_clear()


@pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="未设置 TEST_REDIS_URL")
def test_redis_wake_roundtrip(app_env):
    from app.redis_bus import bus

    assert bus.ping()["ok"]
    assert bus.wake()
    assert bus.wait() is True
    assert bus.wait() is False  # 积压信号已合并清空，第二次等待超时


def test_redis_unavailable_falls_back(app_env, monkeypatch):
    from app.redis_bus import RedisBus

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    config.get_settings.cache_clear()
    b = RedisBus()
    assert b.wake() is False
    assert b.wait() is None
    assert b.ping()["ok"] is False
