"""验收（测试工程师，2026-11-10）：查询向量缓存——键包含服务商 / 模型 / 维度 / 归一化，换任一项不命中；LRU 上限；
缓存的只是向量，不同店铺同一问题命中缓存但各自只检索到自己能用的条款。"""
import pytest

from test_knowledge_upload import upload, xlsx
from test_rag import KB, rag, search  # noqa: F401  （rag 是夹具）


@pytest.mark.parametrize("env, value", [("EMBEDDING_MODEL", "other-model"), ("EMBEDDING_PROVIDER", "other_provider"),
                                        ("EMBEDDING_DIMENSION", "32"), ("EMBEDDING_NORMALIZE", "true")])
def test_cache_key_includes_profile(rag, monkeypatch, env, value):  # noqa: F811
    from app import config
    from app.knowledge import vector_index as vi

    _, fake = rag
    calls = []
    orig = fake.embed_query
    fake.embed_query = lambda t: calls.append(t) or orig(t)
    vi.clear_query_cache()
    v1, c1 = vi.embed_query_cached("where is my parcel")
    v2, c2 = vi.embed_query_cached("where is my parcel")
    assert (c1, c2) == (False, True) and len(calls) == 1 and v1 == v2
    monkeypatch.setenv(env, value)
    config.get_settings.cache_clear()
    _, c3 = vi.embed_query_cached("where is my parcel")
    assert c3 is False and len(calls) == 2, env  # 换了档案的任一项：不用旧向量


def test_cache_lru_limit_and_copy(rag, monkeypatch):  # noqa: F811
    from app.knowledge import vector_index as vi

    vi.clear_query_cache()
    monkeypatch.setattr(vi, "QUERY_CACHE_SIZE", 3)
    for q in ("a1", "a2", "a3"):
        vi.embed_query_cached(q)
    vi.embed_query_cached("a1")  # 用过一次：变成最新
    vi.embed_query_cached("a4")  # 挤掉最久没用的 a2
    assert vi.embed_query_cached("a1")[1] is True and vi.embed_query_cached("a2")[1] is False
    v, _ = vi.embed_query_cached("a3")
    v.append(123.0)  # 调用方改返回值不影响缓存
    assert len(vi.embed_query_cached("a3")[0]) == len(v) - 1
    assert all(isinstance(x, float) for k, val in vi._query_cache.items() for x in val)  # 缓存里只有向量（浮点数）


def test_same_question_other_store_cached_but_scoped(rag):  # noqa: F811
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import KnowledgeChunk

    client, fake = rag
    upload(client, xlsx(KB))
    with session_scope() as db:  # 把一条条款改成只属于店铺 A
        c = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.clause_id == "SHP-1", KnowledgeChunk.review_state == "PUBLISHED"))
        c.store_id = "STORE_A"
        others = {x.chunk_id: x.store_id for x in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.review_state == "PUBLISHED"))}
    calls = []
    orig = fake.embed_query
    fake.embed_query = lambda t: calls.append(t) or orig(t)
    a = search(client, "多久发货？用什么快递")["hits"]
    b = search(client, "多久发货？用什么快递", store_id="STORE_B")["hits"]
    assert len(calls) == 1  # 店铺 B 同一问题：命中缓存（只是查询向量）
    assert any(others[h["chunk_id"]] == "STORE_A" for h in a)
    assert all(others[h["chunk_id"]] in ("ALL", "STORE_B") for h in b)  # 店铺 A 专属条款不会出现在店铺 B
