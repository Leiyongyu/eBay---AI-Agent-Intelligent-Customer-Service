"""测试环境：不读取项目 .env（避免用到真实模型 Key、真实库），假模型通过 override 注入。

默认每个测试用独立的临时 SQLite；设置 TEST_DATABASE_URL（PostgreSQL 连接串）时，
每个测试在该实例上新建一个临时库，结束后删除。TEST_REDIS_URL 同理可选。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import config  # noqa: E402

config.Settings.model_config["env_file"] = None


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("LLM_", "EMBEDDING_", "VISION_")):
            monkeypatch.delenv(key, raising=False)
    for key in ("DATABASE_URL", "DATABASE_PASSWORD", "REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    pg_base = os.environ.get("TEST_DATABASE_URL")
    if pg_base:
        from sqlalchemy.engine import make_url

        url = make_url(pg_base).set(database=f"ebaycs_test_{uuid.uuid4().hex[:8]}")
        monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    else:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    if os.environ.get("TEST_REDIS_URL"):
        monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
        monkeypatch.setenv("REDIS_KEY_PREFIX", f"ebaycs_test_{uuid.uuid4().hex[:6]}:")
    monkeypatch.delenv("KNOWLEDGE_SEED_FILE", raising=False)  # 测试不预导入知识；需要时各测试自己导入样例
    monkeypatch.setenv("MEDIA_DIR", (tmp_path / "media").as_posix())  # 附件写到临时目录，不碰 data/media
    monkeypatch.setenv("KB_DOCS_DIR", (tmp_path / "kb_documents").as_posix())  # 参考文档写到临时目录，不碰 data/kb_documents
    monkeypatch.setenv("MESSAGE_MERGE_WINDOW_SECONDS", "180")
    monkeypatch.setenv("SEND_DELAY_SECONDS", "0")  # 旧测试按“提交即发”验证；延迟发送与撤回见 test_outbox.py
    monkeypatch.setenv("EBAY_CHANNEL", "mock")
    monkeypatch.delenv("EBAY_LIVE_STORES", raising=False)
    config.get_settings.cache_clear()

    from app import db, redis_bus
    from app.providers.chat import set_chat_provider_override
    from app.seed import bootstrap

    db.reset_engine()
    redis_bus.bus.__init__()
    bootstrap()
    yield
    set_chat_provider_override(None)
    from app.providers.vision import set_vision_provider_override

    set_vision_provider_override(None)
    db.reset_engine()
    if pg_base:
        _drop_database(os.environ["DATABASE_URL"])
    config.get_settings.cache_clear()


def _drop_database(url_str: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(url_str)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app.server import create_app

    # 不进入 with 块：不启动 lifespan 里的 Worker 线程，测试中手动调用 worker.run_once()
    c = TestClient(create_app())
    make_user("tester", "ADMIN")
    login(c, "tester")
    return c


TEST_PASSWORD = "Test-pass-123"


def make_user(username, role, stores=(), all_stores=False, display_name=None):
    """测试账号（显示名默认同登录名，审核员署名即显示名）。"""
    from app.auth import hash_password
    from app.db import session_scope
    from app.models import User, UserStore

    with session_scope() as db:
        u = User(username=username, display_name=display_name or username, role=role,
                 password_hash=hash_password(TEST_PASSWORD), all_stores=all_stores)
        db.add(u)
        db.flush()
        for sid in stores:
            db.add(UserStore(user_id=u.id, store_id=sid))
        return u.id


def login(c, username, password=TEST_PASSWORD):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def _clear_goodcang_cache():
    """谷仓出库单缓存在进程内存里，每个测试前清空（goodcang_cache）。"""
    from app.logistics import goodcang

    goodcang.clear_cache()
    yield
