"""数据库引擎与会话。

业务库（DATABASE_URL）：PostgreSQL 或 MySQL（2026-11-12 起计划迁到 MySQL jmh_ai_agent，表名加 TABLE_PREFIX=cs_）；
未配置时回退到本地 SQLite，便于零安装启动。向量库（VECTOR_DATABASE_URL，pgvector）可以单独一个 PostgreSQL，
留空时和业务库同一个（见 get_vector_engine）。表结构只用 SQLAlchemy 的可移植类型，几种数据库共用同一套模型；
MySQL 上：表一律 utf8mb4 + utf8mb4_0900_bin（按字节比较：区分大小写、重音，也不忽略末尾空格，和 PostgreSQL 一致）、DATETIME 带微秒、
“NULLS LAST”改成等价写法（见文件末尾的编译规则）。
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
    """业务表（DATABASE_URL）。"""


class VectorBase(DeclarativeBase):
    """向量库里的表（VECTOR_DATABASE_URL；没单独配置时和业务库同一个）：向量档案。knowledge_embeddings 不在模型里，见 vector_index。"""


def T(name: str) -> str:
    """业务表名：加 TABLE_PREFIX（MySQL 上 cs_）。模型定义时读一次配置。"""
    return (get_settings().table_prefix or "") + name


MYSQL_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_bin"}


def apply_mysql_table_options(metadata) -> None:
    """MySQL：每张表 utf8mb4_0900_bin（库默认的 *_ci 不区分大小写和重音，会让 eBay 编号、买家名这类本来不同的值撞唯一约束）。"""
    for t in metadata.tables.values():
        for k, v in MYSQL_TABLE_OPTIONS.items():
            t.dialect_kwargs.setdefault(k, v)


def utcnow() -> datetime:
    # 统一存无时区的 UTC 时间，SQLite 与 PostgreSQL 行为一致
    return datetime.now(timezone.utc).replace(tzinfo=None)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_vector_engine: Engine | None = None
_VectorSession: sessionmaker[Session] | None = None


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # Worker 线程与请求线程共用一个 SQLite 文件
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    if url.startswith("mysql"):
        # 和 PostgreSQL 一样读已提交；连接闲置超过 MySQL wait_timeout 前回收；会话时区 UTC（时间都存无时区的 UTC）
        return {"isolation_level": "READ COMMITTED", "pool_recycle": 1800,
                "connect_args": {"charset": "utf8mb4", "init_command": "SET time_zone = '+00:00'"}}
    return {}


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().effective_database_url
        _engine = create_engine(url, pool_pre_ping=True, **_engine_kwargs(url))
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_vector_engine() -> Engine:
    """向量库引擎：VECTOR_DATABASE_URL 没配置（或和业务库相同）时就是业务库引擎。"""
    global _vector_engine, _VectorSession
    s = get_settings()
    if not s.vector_database_url or s.effective_vector_database_url == s.effective_database_url:
        return get_engine()
    if _vector_engine is None:
        url = s.effective_vector_database_url
        _vector_engine = create_engine(url, pool_pre_ping=True, **_engine_kwargs(url))
        _VectorSession = sessionmaker(bind=_vector_engine, expire_on_commit=False)
    return _vector_engine


def vector_separate() -> bool:
    return get_vector_engine() is not get_engine()


@contextmanager
def vector_session_scope() -> Iterator[Session]:
    """向量库会话（单独一个库时）；和业务库同一个时也新开一个会话，调用方不要和业务会话混用同一事务。"""
    engine = get_vector_engine()
    maker = _VectorSession if engine is not get_engine() else _SessionLocal
    assert maker is not None
    session = maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """测试用：切换 DATABASE_URL 后重建引擎。"""
    global _engine, _SessionLocal, _vector_engine, _VectorSession
    if _engine is not None:
        _engine.dispose()
    if _vector_engine is not None:
        _vector_engine.dispose()
    _engine = None
    _SessionLocal = None
    _vector_engine = None
    _VectorSession = None
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
    if url.drivername.startswith("mysql"):
        admin = create_engine(url._replace(database=None), **_engine_kwargs(str(url.drivername)))  # set(database=None) 不会清掉库名
        try:
            with admin.connect() as conn:
                exists = conn.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :n"),
                                      {"n": url.database}).scalar()
                if not exists:  # 新建的库默认 utf8mb4_0900_bin；已有的库（如 jmh_ai_agent）不改库默认，表各自指定排序规则
                    conn.execute(text(f"CREATE DATABASE `{url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"))
                    conn.commit()
                    return True
        finally:
            admin.dispose()
        return False
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


def q(engine_or_conn, name: str) -> str:
    """按数据库方言给标识符加引号（PostgreSQL / SQLite 双引号，MySQL 反引号）。"""
    return engine_or_conn.dialect.identifier_preparer.quote(name)


def _sql_literal(text_value: str) -> str:
    # COMMENT ON 不支持绑定参数，只能拼字面量；注释都是代码里的常量，这里只需转义单引号
    return "'" + text_value.replace("'", "''") + "'"


def sync_comments() -> int:
    """PostgreSQL：把模型里的表/字段注释同步成 COMMENT ON（create_all 只给新建的表写注释）。

    只更新与数据库现有注释不一致的项，返回更新条数。SQLite 不支持注释，直接跳过；
    MySQL 建表时注释已经写进 CREATE TABLE，补字段时也带 COMMENT（add_missing_columns），这里跳过。
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
                extra = ""
                if engine.dialect.name == "mysql" and col.comment:
                    extra = " COMMENT " + _sql_literal(col.comment).replace("\\", "\\\\")
                conn.execute(text(f"ALTER TABLE {q(engine, table.name)} ADD COLUMN {q(engine, col.name)} {col_type}{extra}"))
                added.append(f"{table.name}.{col.name}")
    if added:
        logging.getLogger(__name__).info("已为现有表补充字段：%s", "、".join(added))
    return added


def install_credential_guards(engine: Engine | None = None) -> None:
    """店铺凭证表“只能覆盖、不能修改”的数据库级保护（幂等）。

    PostgreSQL：禁止 UPDATE；DELETE 只在导入事务里（SET LOCAL app.credentials_import = 'on'）放行。
    MySQL：禁止 UPDATE；DELETE 只在会话变量 @credentials_import = 'on' 时放行（导入时设、删完立刻清）。
    SQLite（仅本地开发回退）：禁止 UPDATE；SQLite 没有会话变量，DELETE 的限制只能靠代码只有导入一个入口。
    """
    from sqlalchemy import text

    engine = engine or get_engine()
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
            for table in (T("store_credentials"), T("store_credential_imports")):
                conn.execute(text(f'DROP TRIGGER IF EXISTS trg_{table}_guard ON "{table}"'))
                conn.execute(text(f'CREATE TRIGGER trg_{table}_guard BEFORE UPDATE OR DELETE ON "{table}" '
                                  f"FOR EACH ROW EXECUTE FUNCTION store_credentials_guard()"))
        elif engine.dialect.name == "mysql":
            # MySQL：禁止 UPDATE；DELETE 只在导入时放行（导入前 SET @credentials_import = 'on'，删完立刻清掉，见 store_credentials）
            for table in (T("store_credentials"), T("store_credential_imports")):
                conn.execute(text(f"DROP TRIGGER IF EXISTS `trg_{table}_no_update`"))
                conn.execute(text(f"CREATE TRIGGER `trg_{table}_no_update` BEFORE UPDATE ON `{table}` FOR EACH ROW "
                                  f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} 不允许修改，只能整表覆盖导入'"))
                conn.execute(text(f"DROP TRIGGER IF EXISTS `trg_{table}_delete_guard`"))
                conn.execute(text(f"CREATE TRIGGER `trg_{table}_delete_guard` BEFORE DELETE ON `{table}` FOR EACH ROW "
                                  f"BEGIN IF COALESCE(@credentials_import, '') <> 'on' THEN "
                                  f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} 只能在覆盖导入时删除'; END IF; END"))
        elif engine.dialect.name == "sqlite":
            for table in (T("store_credentials"), T("store_credential_imports")):
                conn.execute(text(f'CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON "{table}" '
                                  f"BEGIN SELECT RAISE(ABORT, '{table} 不允许修改，只能整表覆盖导入'); END"))


# 模型里改成可空的已有字段：PostgreSQL 启动时去掉 NOT NULL（SQLite 测试库每次新建，不需要）
RELAX_NOT_NULL = [(T("aftersales_files"), "mail_id")]
# 唯一约束调整（PostgreSQL）：去掉旧的单列唯一，加上新的组合唯一
DROP_UNIQUE = [(T("conversation_marks"), ["conversation_id"])]  # 原来一个会话只能一个标记
ADD_UNIQUE = [(T("conversation_marks"), "uq_conversation_marks_conv_tag", ["conversation_id", "tag_id"])]
# 工作台会话列表批量查询用的索引（create_all 只给新表建索引，已有的表靠这里补；启动时 CREATE INDEX IF NOT EXISTS）
ADD_INDEXES = [
    (T("messages"), "ix_messages_conv_seq", ["conversation_id", "conversation_seq"],
     "会话内按顺序取最后一条消息 / 最后一条买家消息、未读条数（工作台会话列表）"),
    (T("reply_jobs"), "ix_reply_jobs_conv_id", ["conversation_id", "id"], "每个会话最新的回复任务（工作台会话列表）"),
    (T("work_claims"), "ix_work_claims_conversation", ["conversation_id"], "按会话查认领（工作台会话列表）"),
    (T("outbound_messages"), "ix_outbound_messages_message_id", ["message_id"], "按发出的消息找提交人（会话列表的最后回复人）"),
    (T("send_intents"), "ix_send_intents_platform_message_id", ["platform_message_id"], "按 eBay 消息 ID 找发送人（会话列表的最后回复人）"),
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
            cols_sql = ", ".join(q(engine, c) for c in cols)
            if engine.dialect.name == "mysql":  # MySQL 没有 CREATE INDEX IF NOT EXISTS（上面已查过不存在）；索引注释写在 COMMENT
                conn.execute(text(f"CREATE INDEX {q(engine, name)} ON {q(engine, table)} ({cols_sql}) COMMENT {_sql_literal(comment)}"))
            else:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {q(engine, name)} ON {q(engine, table)} ({cols_sql})"))
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
    VectorBase.metadata.create_all(get_vector_engine())  # 向量档案表：在向量库（没单独配置时就是业务库）
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


# ---------------------------------------------------------------- MySQL 编译规则
from sqlalchemy import DateTime  # noqa: E402
from sqlalchemy.dialects.mysql.base import MySQLCompiler  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(DateTime, "mysql")
def _mysql_datetime(type_, compiler, **kw):  # noqa: ANN001
    """DATETIME 默认不存小数秒：统一 DATETIME(6)，和 PostgreSQL 一样到微秒（排序、去重、比较时间不丢精度）。"""
    return "DATETIME(6)"


def _mysql_nulls_last(self, unary, operator_, **kw):  # noqa: ANN001
    """MySQL 不认 NULLS LAST：倒序时空值本来就在最后，直接去掉；正序时先按“是否为空”排。"""
    from sqlalchemy.sql import operators

    inner = unary.element
    text_ = self.process(inner, **kw)
    if getattr(inner, "modifier", None) is operators.desc_op:
        return text_
    return f"({self.process(getattr(inner, 'element', inner), **kw)} IS NULL), {text_}"


def _mysql_nulls_first(self, unary, operator_, **kw):  # noqa: ANN001
    from sqlalchemy.sql import operators

    inner = unary.element
    text_ = self.process(inner, **kw)
    if getattr(inner, "modifier", None) is operators.desc_op:
        return f"({self.process(inner.element, **kw)} IS NOT NULL), {text_}"
    return text_


MySQLCompiler.visit_nulls_last_op_unary_modifier = _mysql_nulls_last
MySQLCompiler.visit_nulls_first_op_unary_modifier = _mysql_nulls_first
