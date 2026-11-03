"""知识检索（RAG）：分词、上传时向量化、混合检索、未就绪不悄悄回退。

向量模型用假实现（按分词结果哈希到固定维度），只验证流程与过滤，不代表真实向量效果。
PostgreSQL + pgvector 的路径用 TEST_DATABASE_URL 指向装了 pgvector 的实例时一并覆盖。
"""
import hashlib
import math

import pytest

from test_knowledge_upload import body, row, upload, xlsx
from test_pipeline import draft_json, send_buyer, use_fake_llm

DIM = 64


class FakeEmbeddings:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0
        self.texts = 0

    def _vec(self, text):
        from app.knowledge.tokenize import tokens

        v = [0.0] * DIM
        for t in tokens(text):
            v[int(hashlib.md5(t.encode()).hexdigest(), 16) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n if any(v) else 1 / math.sqrt(DIM) for x in v]

    def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError("embedding service down")
        self.calls += 1
        self.texts += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture()
def rag(client, monkeypatch, tmp_path):
    from app import config
    from app.knowledge import excel_import, vector_index

    monkeypatch.setattr(excel_import, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("EBAY_STORE_ID", "STORE_A")
    monkeypatch.setenv("KNOWLEDGE_MODE", "retrieval")
    monkeypatch.setenv("EMBEDDING_DIMENSION", str(DIM))
    config.get_settings.cache_clear()
    fake = FakeEmbeddings()
    vector_index.set_embedder_override(fake)
    yield client, fake
    vector_index.set_embedder_override(None)


FIT = row("FIT-1", "刹车片适配", "刹车片适配 2018 款车型，下单前请提供 VIN。", topic="发货")
FIT[10] = "BP-2018"
KB = [
    row("RET-1", "退货期限", "收到货 30 天内可以通过 eBay 申请退货。"),
    row("SHP-1", "发货时效", "付款后下一个工作日用 DHL 发货。", site="EBAY_DE,EBAY_GB", topic="发货"),
    row("SHP-US", "美国发货", "美国站从加州仓库发 USPS。", site="EBAY_US", topic="发货"),
    row("SOP-1", "部分退款上限", "退款最多 30%，需主管批准。", site="全部", audience="仅内部", topic="退款"),
    FIT,
]


def search(client, query, site="EBAY_DE", **kw):
    r = client.post("/internal/knowledge/search", json={"store_id": "STORE_A", "marketplace_id": site, "query": query, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_tokenize_chinese_and_cache_on_project_disk():
    import jieba

    from app.config import DATA_DIR
    from app.knowledge.tokenize import tokens

    toks = tokens("我想申请部分退款，Rückerstattung bitte! SKU: BP-2018")
    assert "退款" in toks and "rückerstattung" in toks and "bp-2018" in toks
    assert "我" not in toks and "bitte" not in toks
    assert str(jieba.dt.tmp_dir).startswith(str(DATA_DIR))  # 词典缓存不写系统临时目录


def test_upload_embeds_before_publish_and_reuses_vectors(rag):
    client, fake = rag
    _, r = upload(client, xlsx(KB))
    # 可对买家说且启用：RET-1、SHP-1（两站同一正文只算一份）、SHP-US、FIT-1；SOP-1 仅内部不向量化
    assert r["index"]["texts"] == 4 and r["index"]["embedded"] == 4
    st = client.get("/internal/knowledge/index/status", params={"store_id": "STORE_A"}).json()
    assert st["reasons"] == [] and st["missing_vectors"] == 0 and st["indexed"] == st["published_public"] == 5

    # 再传一版，只改了一条：只对改动的正文调用向量模型
    fit2 = row("FIT-1", "刹车片适配", "刹车片适配 2018-2020 款车型。", topic="发货")
    changed = [*KB[:-1], fit2]
    _, r = upload(client, xlsx(changed))
    assert r["index"]["embedded"] == 1 and r["index"]["reused"] == 3


def test_hybrid_search_filters_scope(rag):
    client, _ = rag
    upload(client, xlsx(KB))
    hits = search(client, "多久发货？用什么快递")["hits"]
    ids = [h["clause_id"] for h in hits]
    assert ids[0] == "SHP-1"
    assert "SHP-US" not in ids  # 其他站点的条款不参与检索
    assert "SOP-1" not in ids  # 仅内部条款不会给到回复
    assert {"vector_rank", "keyword_rank"} <= set(hits[0])

    sku = search(client, "Passt BP-2018 auf mein Auto?")["hits"]
    assert sku[0]["clause_id"] == "FIT-1" and sku[0]["sku_rank"] == 1
    assert search(client, "多久发货", site="EBAY_US")["hits"][0]["clause_id"] == "SHP-US"
    assert len(search(client, "发货", top_k=1)["hits"]) == 1


def test_embedding_failure_keeps_old_version(rag):
    client, fake = rag
    first, _ = upload(client, xlsx(KB))
    fake.fail = True
    data = xlsx([row("NEW-1", "新条款", "新内容。")])
    p = client.post("/internal/knowledge/upload/preview", json=body(data)).json()
    r = client.post("/internal/knowledge/upload/commit", json=body(data, p["file_sha256"]))
    assert r.status_code == 502 and "旧版本不变" in r.json()["detail"]["message"]
    imports = client.get("/internal/knowledge/imports", params={"store_id": "STORE_A"}).json()
    assert len(imports) == 1 and imports[0]["status"] == "ACTIVE"
    fake.fail = False
    assert search(client, "退货")["hits"][0]["clause_id"] == "RET-1"


def test_not_ready_is_reported_not_silently_skipped(rag, monkeypatch):
    client, _ = rag
    upload(client, xlsx(KB))
    from app.knowledge import vector_index

    vector_index.set_embedder_override(None)  # 没有假模型、也没配 EMBEDDING_* → 未就绪
    r = client.post("/internal/knowledge/search", json={"store_id": "STORE_A", "marketplace_id": "EBAY_DE", "query": "退货"})
    assert r.status_code == 409
    assert any("EMBEDDING" in x for x in r.json()["detail"]["reasons"])
    status = client.get("/api/system/status").json()
    assert status["rag_status"] == "RAG_NOT_READY"


def test_agent_uses_retrieved_clauses(rag):
    client, _ = rag
    upload(client, xlsx(KB))
    use_fake_llm(draft_json())
    from app.db import session_scope
    from app.models import AgentStep
    from app.worker import worker

    from sqlalchemy import select

    send_buyer(client, "When will my order ship? 多久发货")
    worker.run_once()
    with session_scope() as db:
        step = db.scalars(select(AgentStep).where(AgentStep.node == "load_knowledge")).all()[-1]
        assert step.result_status == "OK", step.detail
        assert step.detail["mode"] == "retrieval" and step.detail["hits"]


def test_agent_rag_not_ready_goes_to_human(rag):
    client, _ = rag
    from app.knowledge import vector_index
    from app.worker import worker

    vector_index.set_embedder_override(None)
    use_fake_llm(draft_json())
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = client.get(f"/internal/conversations/{sent['conversation_id']}").json()
    assert "RAG_NOT_READY" in d["job"]["decision_reasons"]
    assert d["drafts"] == []


def test_keyword_channel_ignores_single_generic_word(app_env):
    from app.db import session_scope
    from app.knowledge.tokenize import search_text, tokens
    from app.knowledge.vector_index import keyword_search
    from app.models import KnowledgeChunk

    def chunk(cid, body):
        return KnowledgeChunk(chunk_id=cid, text=body, search_text=search_text(body))

    chunks = [chunk("fit", "适配咨询：请提供 VIN，Teil passt nur mit FIN-Prüfung"),
              chunk("ship", "Versand: Ihre Bestellung wird versendet, Sendungsnummer folgt"),
              chunk("other", "发票随 eBay 消息发送")]
    with session_scope() as db:
        # 只命中一个泛词 teil：不算
        assert keyword_search(db, chunks, tokens("Das Teil ist beschädigt angekommen"), 10) == []
        # 命中 bestellung + versendet：算
        assert [c for c, _ in keyword_search(db, chunks, tokens("Wann wird meine Bestellung versendet?"), 10)] == ["ship"]
