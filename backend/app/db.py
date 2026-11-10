"""数据库引擎与会话。

生产使用 PostgreSQL（DATABASE_URL）；未配置时回退到本地 SQLite，便于零安装启动。
表结构只用 SQLAlchemy 的可移植类型，两种数据库共用同一套模型。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    # 统一存无时区的 UTC 时间，SQLite 与 PostgreSQL 行为一致
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().effective_database_url
        kwargs = {}
        if url.startswith("sqlite"):
            # Worker 线程与请求线程共用一个 SQLite 文件
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, pool_pre_ping=True, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """测试用：切换 DATABASE_URL 后重建引擎。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    from .knowledge import vector_index  # 向量后端状态按引擎缓存，换库后重新检测

    vector_index.reset()


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖。"""
    with session_scope() as s:
        yield s


def ensure_database_exists() -> bool:
    """PostgreSQL：目标库不存在时连到 postgres 库自动创建。返回是否新建。"""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    url = make_url(get_settings().effective_database_url)
    if not url.drivername.startswith("postgresql"):
        return False
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}" ENCODING \'UTF8\''))
                return True
    finally:
        admin.dispose()
    return False


def dialect_name() -> str:
    return get_engine().dialect.name


def _sql_literal(text_value: str) -> str:
    # COMMENT ON 不支持绑定参数，只能拼字面量；注释都是代码里的常量，这里只需转义单引号
    return "'" + text_value.replace("'", "''") + "'"


def sync_comments() -> int:
    """PostgreSQL：把模型里的表/字段注释同步成 COMMENT ON（create_all 只给新建的表写注释）。

    只更新与数据库现有注释不一致的项，返回更新条数。SQLite 不支持注释，直接跳过。
    """
    from sqlalchemy import text

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return 0
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT c.relname, a.attname, d.description
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = current_schema()
            LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = COALESCE(a.attnum, 0)
            WHERE c.relkind = 'r'
            UNION ALL
            SELECT c.relname, NULL, d.description
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = current_schema()
            LEFT JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
            WHERE c.relkind = 'r'
        """)).all()
        existing = {(r[0], r[1]): r[2] for r in rows}
        changed = 0
        for table in Base.metadata.sorted_tables:
            if table.comment and existing.get((table.name, None)) != table.comment:
                conn.execute(text(f'COMMENT ON TABLE "{table.name}" IS {_sql_literal(table.comment)}'))
                changed += 1
            for col in table.columns:
                if col.comment and existing.get((table.name, col.name)) != col.comment:
                    conn.execute(text(f'COMMENT ON COLUMN "{table.name}"."{col.name}" IS {_sql_literal(col.comment)}'))
                    changed += 1
    return changed


def add_missing_columns() -> list[str]:
    """已有表补上模型里新增的可空字段（只加不删不改）。正式引入 Alembic 迁移前的过渡做法。"""
    import logging

    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable or col.primary_key:
                    raise RuntimeError(f"{table.name}.{col.name} 是非空字段，不能自动添加，请写迁移")
                col_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        logging.getLogger(__name__).info("已为现有表补充字段：%s", "、".join(added))
    return added


def install_credential_guards() -> None:
    """店铺凭证表“只能覆盖、不能修改”的数据库级保护（幂等）。

    PostgreSQL：禁止 UPDATE；DELETE 只在导入事务里（SET LOCAL app.credentials_import = 'on'）放行。
    SQLite（仅本地开发回退）：禁止 UPDATE；SQLite 没有会话变量，DELETE 的限制只能靠代码只有导入一个入口。
    """
    from sqlalchemy import text

    engine = get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("""
                CREATE OR REPLACE FUNCTION store_credentials_guard() RETURNS trigger AS $$
                BEGIN
                  IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION '% 不允许修改，只能整表覆盖导入', TG_TABLE_NAME;
                  END IF;
                  IF coalesce(current_setting('app.credentials_import', true), '') <> 'on' THEN
                    RAISE EXCEPTION '% 只能在覆盖导入时删除', TG_TABLE_NAME;
                  END IF;
                  RETURN OLD;
                END $$ LANGUAGE plpgsql
            """))
            for table in ("store_credentials", "store_credential_imports"):
                conn.execute(text(f'DROP TRIGGER IF EXISTS trg_{table}_guard ON "{table}"'))
                conn.execute(text(f'CREATE TRIGGER trg_{table}_guard BEFORE UPDATE OR DELETE ON "{table}" '
                                  f"FOR EACH ROW EXECUTE FUNCTION store_credentials_guard()"))
        elif engine.dialect.name == "sqlite":
            for table in ("store_credentials", "store_credential_imports"):
                conn.execute(text(f'CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON "{table}" '
                                  f"BEGIN SELECT RAISE(ABORT, '{table} 不允许修改，只能整表覆盖导入'); END"))


# 模型里改成可空的已有字段：PostgreSQL 启动时去掉 NOT NULL（SQLite 测试库每次新建，不需要）
RELAX_NOT_NULL = [("aftersales_files", "mail_id")]
# 唯一约束调整（PostgreSQL）：去掉旧的单列唯一，加上新的组合唯一
DROP_UNIQUE = [("conversation_marks", ["conversation_id"])]  # 原来一个会话只能一个标记
ADD_UNIQUE = [("conversation_marks", "uq_conversation_marks_conv_tag", ["conversation_id", "tag_id"])]
# 工作台会话列表批量查询用的索引（create_all 只给新表建索引，已有的表靠这里补；启动时 CREATE INDEX IF NOT EXISTS）
ADD_INDEXES = [
    ("messages", "ix_messages_conv_seq", ["conversation_id", "conversation_seq"],
     "会话内按顺序取最后一条消息 / 最后一条买家消息、未读条数（工作台会话列表）"),
    ("reply_jobs", "ix_reply_jobs_conv_id", ["conversation_id", "id"], "每个会话最新的回复任务（工作台会话列表）"),
    ("work_claims", "ix_work_claims_conversation", ["conversation_id"], "按会话查认领（工作台会话列表）"),
    ("outbound_messages", "ix_outbound_messages_message_id", ["message_id"], "按发出的消息找提交人（会话列表的最后回复人）"),
    ("send_intents", "ix_send_intents_platform_message_id", ["platform_message_id"], "按 eBay 消息 ID 找发送人（会话列表的最后回复人）"),
]


def add_missing_indexes() -> list[str]:
    """补建 ADD_INDEXES 里的索引（已有就跳过），PostgreSQL 上同时写中文注释。"""
    import logging

    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    added = []
    with engine.begin() as conn:
        for table, name, cols, comment in ADD_INDEXES:
            if table not in tables:
                continue
            if any(ix["name"] == name for ix in inspector.get_indexes(table)):
                continue
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})'))
            if engine.dialect.name == "postgresql":
                conn.execute(text(f'COMMENT ON INDEX "{name}" IS {_sql_literal(comment)}'))
            added.append(name)
    if added:
        logging.getLogger(__name__).info("已补建索引：%s", "、".join(added))
    return added


def migrate_unique() -> list[str]:
    import logging

    from sqlalchemy import inspect, text

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    done = []
    with engine.begin() as conn:
        for table, cols in DROP_UNIQUE:
            if table not in tables:
                continue
            for uc in inspector.get_unique_constraints(table):
                if list(uc["column_names"]) == cols:
                    conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{uc["name"]}"'))
                    done.append(f"去掉 {table}({', '.join(cols)}) 唯一约束")
            for ix in inspector.get_indexes(table):
                if ix.get("unique") and list(ix["column_names"]) == cols:
                    conn.execute(text(f'DROP INDEX IF EXISTS "{ix["name"]}"'))
                    done.append(f"去掉 {table}({', '.join(cols)}) 唯一索引")
        for table, name, cols in ADD_UNIQUE:
            if table in tables:
                conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}" ON "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)})'))
    if done:
        logging.getLogger(__name__).info("表结构调整：%s", "；".join(done))
    return done


def relax_not_null() -> list[str]:
    import logging

    from sqlalchemy import inspect, text

    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    done = []
    with engine.begin() as conn:
        for table, column in RELAX_NOT_NULL:
            if table not in tables:
                continue
            col = next((c for c in inspector.get_columns(table) if c["name"] == column), None)
            if col is not None and not col["nullable"]:
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP NOT NULL'))
                done.append(f"{table}.{column}")
    if done:
        logging.getLogger(__name__).info("已把字段改为可空：%s", "、".join(done))
    return done


def init_db() -> None:
    from . import models  # noqa: F401  注册所有表

    ensure_database_exists()
    Base.metadata.create_all(get_engine())
    add_missing_columns()
    relax_not_null()
    migrate_unique()
    add_missing_indexes()
    sync_comments()
    install_credential_guards()
    from .knowledge.vector_index import vector_status

    st = vector_status()  # 建向量表、全文索引；PostgreSQL 未装 pgvector 时只记原因，不阻止启动
    if st.reasons:
        import logging

        logging.getLogger(__name__).warning("知识检索索引未就绪：%s", "；".join(st.reasons))
