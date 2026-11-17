"""知识向量索引与全文索引（第 15.3、15.4 节）。

向量存放在 knowledge_embeddings 表，按（向量档案, 正文哈希）去重：同一条款展开到多个站点、
或重新上传时正文没变，都只向量化一次。不同档案（模型/维度/距离）的向量互不混用。

后端：
  PostgreSQL —— pgvector 的 vector 类型 + 每个档案一个 HNSW（cosine）部分索引；
                 全文检索用 to_tsvector('simple', search_text) + GIN 索引（search_text 是应用侧 jieba 分词结果）。
                 未安装 pgvector 时检索模式直接报 RAG_NOT_READY，不悄悄退回全量注入或内存检索。
  SQLite     —— 仅开发/测试：向量以 JSON 文本存放，相似度在 Python 里算。
这张表不在 SQLAlchemy 模型里：vector 类型依赖扩展，放进 create_all 会让没装 pgvector 的库整体建表失败。

两个库（2026-11-12 起）：向量（knowledge_embeddings、embedding_profiles）在向量库（VECTOR_DATABASE_URL，PostgreSQL），
知识条款（knowledge_chunks）在业务库（可以是 MySQL）；两边只靠正文哈希对应，不跨库联表。
没单独配置向量库时两者是同一个库，行为和以前一样（同一个会话、同一个事务）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import _sql_literal, get_engine, get_vector_engine, vector_separate, vector_session_scope
from ..models import EmbeddingProfile, KnowledgeChunk
from ..providers.embeddings import build_embeddings, embedding_ready
from .tokenize import search_text

log = logging.getLogger(__name__)

TABLE = "knowledge_embeddings"
_COMMENTS = {
    None: "知识向量：按（向量档案, 正文哈希）存一份向量，条款按正文哈希关联；不同档案的向量不混用",
    "profile_id": "向量档案 ID（embedding_profiles.id）",
    "content_hash": "被向量化的正文 SHA-256（条款 text 字段）",
    "embedding": "向量（pgvector vector 类型，维度见档案）",
    "created_at": "生成时间",
}


@dataclass
class VectorStatus:
    backend: str  # pgvector / sqlite / unavailable
    reasons: list[str] = field(default_factory=list)
    version: str | None = None

    @property
    def ready(self) -> bool:
        return not self.reasons


_lock = threading.Lock()
_status: VectorStatus | None = None
_hnsw_done: set[int] = set()
_embedder_override = None


class RagNotReady(RuntimeError):
    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def set_embedder_override(embedder) -> None:
    """测试注入假向量模型（需有 embed_documents / embed_query）。"""
    global _embedder_override
    _embedder_override = embedder
    clear_query_cache()  # 换了模型：缓存的查询向量作废


def _embedder():
    return _embedder_override or build_embeddings()


def reset() -> None:
    global _status
    with _lock:
        _status = None
        _hnsw_done.clear()


@contextmanager
def _vec(db: Session):
    """向量操作用的会话：向量库单独一个时新开（提交在退出时），否则就用调用方的业务会话（同一事务）。"""
    if not vector_separate():
        yield db
        return
    with vector_session_scope() as v:
        yield v


def _chunks_table() -> str:
    return KnowledgeChunk.__tablename__


def text_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 建表与状态
def ensure_chunk_indexes() -> None:
    """业务库里知识条款的索引：PostgreSQL 建全文检索 GIN 索引；各库都建“当前有效版本”的索引。"""
    engine = get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{_chunks_table()}_search_text ON {_chunks_table()} "
                              "USING gin (to_tsvector('simple', coalesce(search_text, '')))"))
        ensure_published_index(conn)


def _ensure_pg() -> VectorStatus:
    engine = get_vector_engine()
    st = VectorStatus(backend="pgvector")
    with engine.begin() as conn:
        if not conn.execute(text("SELECT to_tsvector('simple', '退款')::text <> ''")).scalar():
            st.reasons.append("数据库区域设置不把中文当作词，全文检索会漏掉中文（换 UTF-8 / 中文 locale 的库）")
        available = conn.execute(text("SELECT default_version, installed_version FROM pg_available_extensions "
                                      "WHERE name = 'vector'")).first()
    if available is None:
        st.backend = "unavailable"
        st.reasons.append("PostgreSQL 未安装 pgvector 扩展文件（按开发文档 21 章步骤编译安装到 PostgreSQL 目录）")
        return st
    try:
        with engine.begin() as conn:
            if available[1] is None:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            st.version = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    profile_id integer NOT NULL REFERENCES embedding_profiles(id),
                    content_hash varchar(64) NOT NULL,
                    embedding vector NOT NULL,
                    created_at timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
                    PRIMARY KEY (profile_id, content_hash)
                )"""))
            for col, comment in _COMMENTS.items():
                target = f"TABLE {TABLE}" if col is None else f"COLUMN {TABLE}.{col}"
                conn.execute(text(f"COMMENT ON {target} IS {_sql_literal(comment)}"))
    except Exception as e:  # 常见：数据库账号没有建扩展的权限
        st.backend = "unavailable"
        st.reasons.append(f"启用 pgvector 失败：{type(e).__name__}: {str(e).splitlines()[0][:200]}")
    return st


PUBLISHED_INDEX = "ix_knowledge_chunks_published"
PUBLISHED_INDEX_COMMENT = ("起草取知识用的部分索引：只含 review_state=PUBLISHED（当前有效版本）的条款，按店铺、站点、可见级别；"
                           "表里大多是 RETIRED / WITHDRAWN 的旧版本，不用整表扫")


def ensure_published_index(conn) -> None:
    """已有的库建表时没有这个索引（create_all 只给新表建索引）：启动时幂等补建（模型里也声明了，新库建表时就有）。
    MySQL 不支持部分索引，也没有 CREATE INDEX IF NOT EXISTS：建表时模型已建普通索引（没有 WHERE），这里不再补。"""
    if conn.dialect.name == "mysql":
        return
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {PUBLISHED_INDEX} ON {_chunks_table()} (store_id, marketplace_id, audience) "
                      "WHERE review_state = 'PUBLISHED'"))
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"COMMENT ON INDEX {PUBLISHED_INDEX} IS {_sql_literal(PUBLISHED_INDEX_COMMENT)}"))


def _ensure_sqlite() -> VectorStatus:
    with get_vector_engine().begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                profile_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                embedding TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (profile_id, content_hash)
            )"""))
    return VectorStatus(backend="sqlite")


def vector_status(refresh: bool = False) -> VectorStatus:
    """建表并返回向量后端状态；结果缓存，refresh=True 重新检测（装好 pgvector 后不用重启）。"""
    global _status
    with _lock:
        if _status is None or refresh:
            dialect = get_vector_engine().dialect.name
            try:
                ensure_chunk_indexes()
                if dialect == "postgresql":
                    _status = _ensure_pg()
                elif dialect == "sqlite":
                    _status = _ensure_sqlite()
                else:
                    _status = VectorStatus(backend="unavailable",
                                           reasons=[f"向量库不能是 {dialect}：请把 VECTOR_DATABASE_URL 配成装了 pgvector 的 PostgreSQL"])
            except Exception as e:
                _status = VectorStatus(backend="unavailable", reasons=[f"向量索引初始化失败：{type(e).__name__}: {e}"])
        return _status


def readiness(require_embedding: bool = True) -> list[str]:
    reasons = list(vector_status().reasons)
    if require_embedding and _embedder_override is None:
        reasons += embedding_ready()[1]
    return reasons


def require_ready() -> None:
    reasons = readiness()
    if reasons:
        raise RagNotReady(reasons)


# ---------------------------------------------------------------- 档案
def profile_id(db: Session) -> int:
    with _vec(db) as v:
        return current_profile(v).id


def current_profile(db: Session) -> EmbeddingProfile:
    """db 必须是向量库的会话（模块内部用 _vec 取）。"""
    s = get_settings()
    key = dict(provider=s.embedding_provider or "fake", model=s.embedding_model or "fake",
               dimension=int(s.embedding_dimension or 0), distance=s.embedding_distance,
               normalize=bool(s.embedding_normalize))
    if key["distance"] != "cosine":
        raise RagNotReady([f"EMBEDDING_DISTANCE={key['distance']} 暂不支持（只支持 cosine）"])
    profile = db.scalar(select(EmbeddingProfile).filter_by(**key))
    if profile is None:
        profile = EmbeddingProfile(**key)
        db.add(profile)
        db.flush()
    if db.get_bind().dialect.name == "postgresql" and profile.id not in _hnsw_done:
        # 每个档案一个部分索引：vector 列不定维，按档案维度强转后建 HNSW
        db.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_hnsw_p{profile.id} ON {TABLE} USING hnsw "
                        f"((embedding::vector({profile.dimension})) vector_cosine_ops) "
                        f"WHERE profile_id = {profile.id}"))
        _hnsw_done.add(profile.id)
    return profile


# ---------------------------------------------------------------- 写入
def fill_search_text(chunks) -> int:
    n = 0
    for c in chunks:
        if c.search_text is None:
            c.search_text = search_text(c.text)
            n += 1
    return n


def existing_hashes(db: Session, profile_id: int, hashes: list[str]) -> set[str]:
    with _vec(db) as v:
        return _existing_hashes(v, profile_id, hashes)


def _existing_hashes(db: Session, profile_id: int, hashes: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        params = {f"h{j}": h for j, h in enumerate(part)}
        marks = ", ".join(f":h{j}" for j in range(len(part)))
        rows = db.execute(text(f"SELECT content_hash FROM {TABLE} WHERE profile_id = :p AND content_hash IN ({marks})"),
                          {"p": profile_id, **params}).scalars()
        found.update(rows)
    return found


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def embed_texts(db: Session, bodies: list[str]) -> dict:
    """确保这些正文都有当前档案的向量；只对缺的调用向量模型。失败直接抛出，由调用方决定是否中止。"""
    require_ready()
    with _vec(db) as v:
        return _embed_texts(db, v, bodies)


def _embed_texts(db: Session, v: Session, bodies: list[str]) -> dict:
    """db：业务会话（记用量）；v：向量会话（档案、向量）。"""
    profile = current_profile(v)
    by_hash = {text_hash(b): b for b in bodies if b}
    missing = [h for h in by_hash if h not in _existing_hashes(v, profile.id, list(by_hash))]
    if missing:
        from .. import usage as usage_log
        from .retriever import estimate_tokens

        started = time.perf_counter()
        vectors = _embedder().embed_documents([by_hash[h] for h in missing])
        s = get_settings()
        usage_log.record(db, "embedding_docs", provider=s.embedding_provider, model=s.embedding_model,
                         estimated_tokens=sum(estimate_tokens(by_hash[h]) for h in missing),
                         duration_ms=int((time.perf_counter() - started) * 1000))
        if len(vectors) != len(missing):
            raise ValueError(f"向量模型返回 {len(vectors)} 条，请求 {len(missing)} 条")
        pg = v.get_bind().dialect.name == "postgresql"
        for h, vec in zip(missing, vectors):
            if len(vec) != profile.dimension:
                raise ValueError(f"实际维度 {len(vec)} 与 EMBEDDING_DIMENSION={profile.dimension} 不一致，停止写入")
            if profile.normalize:
                vec = _normalize(vec)
            if pg:
                v.execute(text(f"INSERT INTO {TABLE} (profile_id, content_hash, embedding) "
                               f"VALUES (:p, :h, CAST(:v AS vector)) ON CONFLICT DO NOTHING"),
                          {"p": profile.id, "h": h, "v": "[" + ",".join(f"{x:.7g}" for x in vec) + "]"})
            else:
                v.execute(text(f"INSERT OR IGNORE INTO {TABLE} (profile_id, content_hash, embedding) "
                               f"VALUES (:p, :h, :v)"), {"p": profile.id, "h": h, "v": json.dumps(vec)})
        v.flush()
    return {"profile_id": profile.id, "texts": len(by_hash), "embedded": len(missing), "reused": len(by_hash) - len(missing)}


def index_chunks(db: Session, chunks: list[KnowledgeChunk]) -> dict:
    """条款建索引：补分词；可对买家说的条款生成向量（仅内部条款不进回复检索，不花向量化费用）。"""
    fill_search_text(chunks)
    public = [c.text for c in chunks if c.audience == "PUBLIC_REPLY"]
    return embed_texts(db, public)


def index_status(db: Session, store_id: str) -> dict:
    chunks = db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.store_id.in_([store_id, "ALL"]), KnowledgeChunk.review_state == "PUBLISHED",
        KnowledgeChunk.audience == "PUBLIC_REPLY")).all()
    st = vector_status()
    out = {"backend": st.backend, "pgvector_version": st.version, "reasons": readiness(),
           "published_public": len(chunks), "missing_search_text": sum(c.search_text is None for c in chunks),
           "indexed": None, "missing_vectors": None, "profile": None}
    if st.ready:
        with _vec(db) as v:
            profile = current_profile(v)
            have = _existing_hashes(v, profile.id, list({text_hash(c.text) for c in chunks}))
        out.update(indexed=sum(text_hash(c.text) in have for c in chunks),
                   missing_vectors=sum(text_hash(c.text) not in have for c in chunks),
                   profile={"id": profile.id, "model": profile.model, "dimension": profile.dimension})
    return out


# ---------------------------------------------------------------- 查询
def embed_query(query: str) -> list[float]:
    v = _embedder().embed_query(query)
    return _normalize(v) if get_settings().embedding_normalize else v


# 查询向量缓存（进程内 LRU）：同一段问题（重新生成、合并窗口内重跑）不重复调用向量模型。
# 键 = 向量档案（服务商、模型、维度、是否归一化）+ 问题文本 SHA-256；换模型或维度自然不命中；注入 / 换掉假模型时清空。
QUERY_CACHE_SIZE = 512
_query_cache: "OrderedDict[tuple, list[float]]" = OrderedDict()
_query_cache_lock = threading.Lock()


def embed_query_cached(query: str) -> tuple[list[float], bool]:
    """返回 (查询向量, 是否来自缓存)。"""
    s = get_settings()
    key = (s.embedding_provider, s.embedding_model, int(s.embedding_dimension or 0), bool(s.embedding_normalize),
           text_hash(query))
    with _query_cache_lock:
        hit = _query_cache.get(key)
        if hit is not None:
            _query_cache.move_to_end(key)
            return list(hit), True
    v = embed_query(query)
    with _query_cache_lock:
        _query_cache[key] = list(v)
        while len(_query_cache) > QUERY_CACHE_SIZE:
            _query_cache.popitem(last=False)
    return v, False


def clear_query_cache() -> None:
    with _query_cache_lock:
        _query_cache.clear()


def vector_search(db: Session, query_vec: list[float], hashes: list[str], limit: int) -> list[tuple[str, float]]:
    """在给定正文哈希范围内按 cosine 距离取最近的 limit 个，返回 [(content_hash, distance)]。"""
    if not hashes:
        return []
    with _vec(db) as v:
        return _vector_search(v, query_vec, hashes, limit)


def _vector_search(db: Session, query_vec: list[float], hashes: list[str], limit: int) -> list[tuple[str, float]]:
    profile = current_profile(db)
    if len(query_vec) != profile.dimension:
        raise ValueError(f"查询向量维度 {len(query_vec)} 与档案 {profile.dimension} 不一致")
    if db.get_bind().dialect.name == "postgresql":
        dim = int(profile.dimension)
        rows = db.execute(text(
            f"SELECT content_hash, (embedding::vector({dim})) <=> CAST(:q AS vector({dim})) AS d FROM {TABLE} "
            f"WHERE profile_id = :p AND content_hash = ANY(:hs) ORDER BY d LIMIT :k"),
            {"q": "[" + ",".join(f"{x:.7g}" for x in query_vec) + "]", "p": profile.id, "hs": hashes, "k": limit}).all()
        return [(r[0], float(r[1])) for r in rows]
    qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
    scored = []
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        params = {f"h{j}": h for j, h in enumerate(part)}
        marks = ", ".join(f":h{j}" for j in range(len(part)))
        for h, raw in db.execute(text(f"SELECT content_hash, embedding FROM {TABLE} "
                                      f"WHERE profile_id = :p AND content_hash IN ({marks})"),
                                 {"p": profile.id, **params}).all():
            v = json.loads(raw)
            dot = sum(a * b for a, b in zip(query_vec, v))
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            scored.append((h, 1.0 - dot / (qn * vn)))
    scored.sort(key=lambda x: x[1])
    return scored[:limit]


# 关键词一路只算“像样的命中”：至少命中 2 个有效词（查询只有 1~2 个词时全中），且覆盖查询词 idf 权重的 25%。
# 否则一个泛词（如 teil、artikel）就能把不相关的条款顶到前面——知识库多为中文、买家多用德语/英语，这一路本来就弱。
KEYWORD_MIN_MATCHED = 2
KEYWORD_MIN_COVERAGE = 0.25


def keyword_search(db: Session, chunks: list[KnowledgeChunk], query_tokens: list[str], limit: int) -> list[tuple[str, float]]:
    """全文检索：返回 [(chunk_id, score)]，分数越大越相关。

    PostgreSQL 先用 GIN 全文索引找出命中任一查询词的条款；打分（idf 加权覆盖度）两种库都在应用侧算，
    这样阈值行为一致，也不依赖 ts_rank 对短文本的打分特性。
    """
    if not chunks or not query_tokens:
        return []
    docs = {c.chunk_id: set((c.search_text or "").split()) for c in chunks}
    if db.get_bind().dialect.name == "postgresql":
        tsq = " | ".join(f"'{t}'" for t in query_tokens[:64])
        matched_ids = set(db.execute(text(
            f"SELECT chunk_id FROM {_chunks_table()} "
            "WHERE chunk_id = ANY(:ids) AND to_tsvector('simple', coalesce(search_text, '')) @@ to_tsquery('simple', :tsq)"),
            {"tsq": tsq, "ids": list(docs)}).scalars())
    else:
        matched_ids = {cid for cid, words in docs.items() if words & set(query_tokens)}
    n = len(docs)
    idf = {t: math.log(1 + n / (1 + sum(t in d for d in docs.values()))) for t in query_tokens}
    total = sum(idf.values()) or 1.0
    need = min(KEYWORD_MIN_MATCHED, len(query_tokens))
    scored = []
    for cid in matched_ids:
        hit = [t for t in query_tokens if t in docs[cid]]
        score = sum(idf[t] for t in hit)
        if len(hit) >= need and score / total >= KEYWORD_MIN_COVERAGE:
            scored.append((cid, score))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]
