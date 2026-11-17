"""评测时切到独立的 SQLite 评测库、关掉一切外部连接和自动动作（和 scripts/eval_intents.py 同一套做法）。

用 config.set_overrides 设（不是改设置对象）：bootstrap() 里 runtime_settings.load() 会清掉 get_settings 缓存，
直接改对象的值会全部回到 .env（2026-11-09 第一次完整评测因此真的发了 2 张飞书工单、注册了 17TRACK）。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

EVAL_OFF = {"redis_url": None, "ebay_sync_enabled": False, "ebay_push_enabled": False, "ebay_channel": "mock",
            "ebay_client_id": None, "ebay_client_secret": None, "ebay_refresh_token": None,
            "feishu_app_id": None, "feishu_app_secret": None, "knowledge_seed_file": None, "demo_seed": False,
            "business_source": "fixture", "knowledge_mode": "full_context", "auto_translate_buyer_messages": False,
            "auto_send_enabled": False, "review_ack_enabled": False, "track17_enabled": False, "track17_api_key": None,
            "goodcang_app_token": None, "goodcang_app_key": None, "feishu_case_enabled": False, "feishu_case_auto_send": False,
            "logistics_ticket_auto_draft": False, "logistics_ticket_auto_submit": False, "logistics_ticket_auto_send": False,
            "logistics_ticket_submit_notice": False, "invoice_auto_generate": False, "invoice_auto_send": False,
            "ship_notice_enabled": False, "mail_enabled": False, "aftersales_mail_enabled": False,
            "eval_gate_on_startup": False, "eval_judge_enabled": False}
KEEP = ("knowledge_mode", "business_source", "ebay_channel")


def check() -> None:
    """切完再核对：有外部连接或开关没关、库不是评测库，就停下。"""
    from ..config import get_settings

    s = get_settings()
    bad = [k for k, v in EVAL_OFF.items() if hasattr(s, k) and k not in KEEP and getattr(s, k) not in (None, False, "")]
    if s.ebay_channel != "mock":
        bad.append("ebay_channel")
    if "eval.db" not in (s.effective_database_url or ""):
        bad.append("database_url")
    if bad:
        raise RuntimeError("评测环境没有隔离干净，已停止：" + "、".join(bad))


@contextlib.contextmanager
def isolated_db(out_dir: Path):
    """进入时切到 out_dir/eval.db（全新）并初始化表；退出时还原配置、重连回原来的库。"""
    from .. import config
    from .. import db as dbmod

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "eval.db"
    if path.exists():
        path.unlink()
    fields = set(config.Settings.model_fields)
    vec = out_dir / "vectors.db"
    if vec.exists():
        vec.unlink()
    values = {k: v for k, v in {**EVAL_OFF, "database_url": f"sqlite:///{path.as_posix()}", "database_password": None,
                                "vector_database_url": f"sqlite:///{vec.as_posix()}", "vector_database_password": None}.items()
              if k in fields}
    prev = dict(config._overrides)
    config.set_overrides({**prev, **values})
    dbmod.reset_engine()
    try:
        from ..seed import bootstrap

        bootstrap()
        config.set_overrides({**config._overrides, **values})  # bootstrap 会用评测库的页面配置重设覆盖值：再盖一次
        check()
        yield path
    finally:
        config.set_overrides(prev)
        dbmod.reset_engine()
