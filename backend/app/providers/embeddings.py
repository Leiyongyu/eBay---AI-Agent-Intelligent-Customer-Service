"""EmbeddingProvider（第 15.1、15.3 节）。

按 EMBEDDING_* 独立构造，不继承 LLM 配置。当前知识走全量注入，向量只在检索模式（尚未实现）入库；
这里先提供能力检查：用固定脱敏文本调用一次，比较实际维度与 EMBEDDING_DIMENSION，不一致即报错。
"""
from __future__ import annotations

from ..config import get_settings

PROBE_TEXT = "刹车片适配 2018 款车型吗？Does this brake pad fit my car?"
SUPPORTED = {"openai_compatible"}
# pgvector 的 vector 类型 HNSW 索引最多 2000 维（第 15.3 节）
PGVECTOR_HNSW_MAX_DIM = 2000


def embedding_ready() -> tuple[bool, list[str]]:
    s = get_settings()
    if not s.llm_enabled:
        return False, ["LLM_ENABLED=false：已停用全部大模型"]
    problems = [f"{name} 未配置" for name, v in (
        ("EMBEDDING_PROVIDER", s.embedding_provider),
        ("EMBEDDING_MODEL", s.embedding_model),
        ("EMBEDDING_API_KEY", s.embedding_api_key),
        ("EMBEDDING_DIMENSION", s.embedding_dimension),
    ) if not v]
    if s.embedding_provider and s.embedding_provider not in SUPPORTED:
        problems.append(f"EMBEDDING_PROVIDER={s.embedding_provider} 暂不支持")
    if s.embedding_base_url and "{" in s.embedding_base_url:
        problems.append("EMBEDDING_BASE_URL 里还有未替换的 {…} 占位符")
    if s.embedding_dimension and s.embedding_dimension > PGVECTOR_HNSW_MAX_DIM:
        problems.append(f"EMBEDDING_DIMENSION={s.embedding_dimension} 超过 pgvector HNSW 的 {PGVECTOR_HNSW_MAX_DIM} 维上限")
    return not problems, problems


def build_embeddings():
    from langchain_openai import OpenAIEmbeddings

    s = get_settings()
    kwargs = {
        "model": s.embedding_model,
        "api_key": s.embedding_api_key,
        "dimensions": s.embedding_dimension,
        # 非 OpenAI 模型不能按 tiktoken 先切 token 再发 token id，必须直接发原文
        "check_embedding_ctx_length": False,
        # 百炼文本向量一次最多 20 条
        "chunk_size": 20,
        "max_retries": 1,
    }
    if s.embedding_base_url:
        kwargs["base_url"] = s.embedding_base_url
    return OpenAIEmbeddings(**kwargs)


def probe() -> dict:
    """用固定文本调用一次，返回实际维度；与配置不一致时抛 ValueError。"""
    ok, problems = embedding_ready()
    if not ok:
        raise ValueError("；".join(problems))
    vector = build_embeddings().embed_query(PROBE_TEXT)
    expected = get_settings().embedding_dimension
    if len(vector) != expected:
        raise ValueError(f"实际维度 {len(vector)} 与 EMBEDDING_DIMENSION={expected} 不一致，停止使用")
    return {"dimension": len(vector), "model": get_settings().embedding_model}
