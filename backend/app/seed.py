"""启动初始化：建表、导入知识库、写入演示店铺。全部幂等，可重复执行。"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from .config import PROJECT_ROOT, get_settings
from .db import init_db, session_scope
from .knowledge.loader import import_jsonl
from .ebay.html_text import convert_stored
from .media.service import cleanup_expired
from .models import Store
from .sources.fixture import FixtureSource

log = logging.getLogger(__name__)


DEFAULT_MESSAGE_TAGS = [("查适配中", "warning"), ("不适配", "danger"), ("已适配", "success"), ("等买家回复", "info"),
                        ("等仓库/物流反馈", "warning"), ("已处理", "success")]


def bootstrap() -> dict:
    init_db()
    from . import runtime_settings

    runtime_settings.load()  # 系统配置页里改过的项优先于 .env
    s = get_settings()
    report: dict = {}
    with session_scope() as db:
        from .models import MessageTag

        if db.scalar(select(func.count(MessageTag.id))) == 0:  # 消息标记的默认选项，管理员可在工作台增改停用
            for i, (name, color) in enumerate(DEFAULT_MESSAGE_TAGS):
                db.add(MessageTag(name=name, color=color, sort=(i + 1) * 10, created_by="初始化"))
            report["message_tags"] = len(DEFAULT_MESSAGE_TAGS)
        if s.demo_seed:
            for row in FixtureSource().stores():
                store = db.get(Store, row["id"])
                if store is None:
                    db.add(Store(id=row["id"], name=row["name"], ebay_user_id=row["ebay_user_id"],
                                 timezone=row["timezone"], default_marketplace_id=row["default_marketplace_id"]))
            report["stores"] = "demo"
        if not s.ebay_not_ready_reasons():
            from .ebay.sync import ensure_ebay_store

            ensure_ebay_store(db)
            report["ebay_store"] = s.ebay_store_id
        if s.knowledge_seed_file:
            path = (PROJECT_ROOT / s.knowledge_seed_file).resolve()
            if path.exists():
                report["knowledge"] = import_jsonl(db, path)
            else:
                log.warning("知识库文件不存在：%s", path)
                report["knowledge"] = f"missing: {s.knowledge_seed_file}"
        # 附件按保留期删除（第 18.2 节）；每次启动清理一次
        report["media_expired"] = cleanup_expired(db)
        # 升级前同步进来的 HTML 消息（eBay 系统通知）转成纯文本，原文挪到 body_html；幂等
        report["html_messages_converted"] = convert_stored(db)
    return report
