"""表结构约定：每张表、每个字段都必须有中文注释；PostgreSQL 上注释要真正写进数据库。"""
import os

import pytest

from app import models  # noqa: F401  注册所有表
from app.db import Base


def test_every_table_and_column_has_comment():
    missing = []
    for table in Base.metadata.sorted_tables:
        if not table.comment:
            missing.append(table.name)
        missing += [f"{table.name}.{c.name}" for c in table.columns if not c.comment]
    assert not missing, "以下表/字段缺少 comment：" + "、".join(missing)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="未设置 TEST_DATABASE_URL")
def test_comments_written_to_postgresql(app_env):
    from sqlalchemy import text

    from app.db import get_engine, sync_comments

    with get_engine().connect() as conn:
        table_comment = conn.execute(text("SELECT obj_description('reply_jobs'::regclass, 'pg_class')")).scalar()
        col_comment = conn.execute(text(
            "SELECT col_description('reply_jobs'::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = 'reply_jobs'::regclass AND attname = 'state'")).scalar()
        missing = conn.execute(text("""
            SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = current_schema()
            WHERE c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
              AND col_description(c.oid, a.attnum) IS NULL
        """)).scalar()
    assert table_comment.startswith("回复任务")
    assert col_comment.startswith("任务状态")
    assert missing == 0
    assert sync_comments() == 0  # 已同步时再次执行不做任何修改
