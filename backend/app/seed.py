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


# 会话标记的默认选项（名称, 颜色 #rrggbb / none 无色），顺序即排序
DEFAULT_MESSAGE_TAGS = [
    ("已好评", "#a78bcf"), ("议价", "#f5b90a"), ("物流异常退回后需要重发", "#26a69a"), ("不适配联系取消", "none"),
    ("周一需要发到售后群", "none"), ("地址问题", "#f59ac0"), ("需跟进/回复", "#d1008a"), ("物流异常", "#5b8def"),
    ("快过期/过期", "#5e6b8c"), ("缺货联系", "#d1008a"), ("要发票", "#f59ac0"), ("查件", "#26a69a"), ("退货", "#f2462e"),
    ("黑名单", "#5e6b8c"), ("查物流", "#26a69a"), ("更新物流单号", "#5dd6a8"), ("自退回", "#ef6a50"),
    ("待咨询群回复", "#f5a055"), ("差评纠纷", "#f2462e"), ("产品问题", "#13a3ad"), ("A待确认适配", "#73c6ec"), ("要VIN", "#0aa2f2"),
]


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
        from .message_templates import seed_system

        if n := seed_system(db):  # 系统预定义的消息模板（只在一条都没有时写入）
            report["message_templates"] = n
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
