"""知识检索提速（2026-11-10）：数据库侧先过滤当前有效版本，结果与“整表读出再逐条判断”完全一致；
查询向量按文本缓存，同一段问题不重复调用向量模型；PUBLISHED 部分索引存在。"""
from datetime import date
from itertools import product

from test_knowledge_upload import upload, xlsx
from test_rag import KB, rag, search  # noqa: F401  （rag 是夹具）


def _chunk(i, **kw):
    from app.models import KnowledgeChunk

    base = dict(chunk_id=f"K{i}", clause_id=f"C{i}", document_id="D", version="1", title=f"t{i}", text=f"body {i}",
                store_id="S1", marketplace_id="ALL", audience="PUBLIC_REPLY", language="de", topic="x", priority=10,
                effective_at="2026-01-01", expires_at=None, review_state="PUBLISHED", content_hash=f"h{i}")
    return KnowledgeChunk(**{**base, **kw})


def test_db_filter_matches_python_filter(app_env):
    from sqlalchemy import select

    from app.db import session_scope
    from app.knowledge.retriever import eligible_chunks, exclusion_reason
    from app.models import KnowledgeChunk

    variants = list(product(
        ["PUBLISHED", "RETIRED", "WITHDRAWN", "DRAFT"],
        ["S1", "S2", "ALL"],
        ["ALL", "EBAY_DE", "EBAY_GB"],
        ["PUBLIC_REPLY", "INTERNAL_SOP"],
        [("2026-01-01", None), ("2026-11-10", ""), ("2026-12-01", None), ("2026-01-01", "2026-11-09"),
         ("2026-01-01", "2026-11-10"), ("2026-11-11", "2027-01-01")],
    ))
    with session_scope() as db:
        for i, (state, store, site, aud, (eff, exp)) in enumerate(variants):
            db.add(_chunk(i, review_state=state, store_id=store, marketplace_id=site, audience=aud,
                          effective_at=eff, expires_at=exp))
    with session_scope() as db:
        rows = db.scalars(select(KnowledgeChunk)).all()
        for store, mk, aud in product(["S1", "S2", "S3"], ["EBAY_DE", "EBAY_GB", "EBAY_US", "UNKNOWN"],
                                      ["PUBLIC_REPLY", "INTERNAL_SOP"]):
            on = date(2026, 11, 10)
            expect = sorted(c.chunk_id for c in rows if exclusion_reason(c, store, mk, on, aud) is None)
            got = sorted(c.chunk_id for c in eligible_chunks(db, store, mk, on, aud))
            assert got == expect and expect, (store, mk, aud)


def test_query_vector_cached_for_same_question(rag):  # noqa: F811
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.knowledge import vector_index
    from app.models import LlmUsage

    client, fake = rag
    upload(client, xlsx(KB))
    calls = []
    orig = fake.embed_query
    fake.embed_query = lambda t: calls.append(t) or orig(t)

    first = search(client, "多久发货？用什么快递")["hits"]
    second = search(client, "多久发货？用什么快递")["hits"]  # 重新生成：同一段问题
    assert first == second and len(calls) == 1
    search(client, "能退货吗")
    assert len(calls) == 2
    with session_scope() as db:  # 用缓存时不记向量费用
        assert db.scalar(select(func.count()).select_from(LlmUsage).where(LlmUsage.purpose == "embedding_query")) == 2
    vector_index.set_embedder_override(fake)  # 换模型：缓存清空
    search(client, "多久发货？用什么快递")
    assert len(calls) == 3


def test_published_partial_index_exists(app_env):
    from sqlalchemy import inspect

    from app.db import get_engine
    from app.knowledge.vector_index import PUBLISHED_INDEX, vector_status

    vector_status(refresh=True)  # 已有的库启动时补建（幂等）
    vector_status(refresh=True)
    names = {ix["name"] for ix in inspect(get_engine()).get_indexes("knowledge_chunks")}
    assert PUBLISHED_INDEX in names
