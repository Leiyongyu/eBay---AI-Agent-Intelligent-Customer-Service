"""正式知识库维护：下载 Excel 模板、上传预览、整体替换、历史版本与回滚（第 5.4、12.3 节）。

上传的文件以 base64 放在 JSON 里传输（不额外依赖 multipart 解析库）。
提交前必须先预览：提交时带上预览返回的文件 SHA-256，服务端重新解析校验，确认是同一个文件且没有错误。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..knowledge import excel_import as xi
from ..knowledge import vector_index as vi
from ..knowledge.retriever import retrieve
from ..models import KnowledgeChunk, KnowledgeImport, Store
from .deps import iso, reviewer_name

from ..auth import require_admin  # noqa: E402

router = APIRouter(prefix="/internal/knowledge", tags=["knowledge"])


def store_sites(db: Session, store_id: str) -> list[str]:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(404, f"店铺 {store_id} 不存在")
    s = get_settings()
    sites = {store.default_marketplace_id}
    if store_id == s.ebay_store_id:
        sites |= set(s.ebay_marketplace_set)
    return sorted(sites)


@router.get("/template", dependencies=[Depends(require_admin)])
def download_template(store_id: str, db: Session = Depends(get_db)):
    data = xi.build_template(store_sites(db, store_id))
    name = f"知识库模板_{store_id}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


class UploadBody(BaseModel):
    store_id: str
    file_name: str = Field(max_length=255)
    data_base64: str
    expected_sha256: str | None = None  # 提交时必填：预览返回的文件哈希


def _decode(body: UploadBody) -> bytes:
    raw = body.data_base64.split(",", 1)[1] if body.data_base64.startswith("data:") else body.data_base64
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "文件内容不是有效的 base64") from None
    if not data:
        raise HTTPException(422, "文件是空的")
    return data


def _parse(db: Session, body: UploadBody):
    data = _decode(body)
    sites = store_sites(db, body.store_id)
    try:
        parsed = xi.parse_workbook(data, sites)
    except xi.ExcelImportError as e:
        raise HTTPException(422, {"message": str(e)}) from None
    preview = xi.preview(db, body.store_id, parsed, sites, get_settings().knowledge_full_context_max_tokens)
    preview["file_sha256"] = hashlib.sha256(data).hexdigest()
    preview["sites"] = sites
    return data, parsed, preview


@router.post("/upload/preview", dependencies=[Depends(require_admin)])
def upload_preview(body: UploadBody, db: Session = Depends(get_db)):
    """只解析校验，不写库。"""
    return _parse(db, body)[2]


@router.post("/upload/commit", dependencies=[Depends(require_admin)])
def upload_commit(body: UploadBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    data, parsed, preview = _parse(db, body)
    if not body.expected_sha256 or body.expected_sha256 != preview["file_sha256"]:
        raise HTTPException(409, "文件与预览时不一致，请重新预览后再提交")
    if parsed.errors:
        raise HTTPException(422, {"message": "还有错误未修正", "errors": parsed.errors})
    summary = {k: preview[k] for k in ("counts", "site_tokens", "budget_tokens", "diff", "warnings")}
    try:
        imp = xi.commit(db, body.store_id, parsed, body.file_name, data, reviewer, summary)
    except vi.RagNotReady as e:
        raise HTTPException(409, {"message": "检索索引未就绪，本次上传未生效（旧版本不变）", "reasons": e.reasons}) from None
    except Exception as e:  # 向量模型调用失败等：事务回滚，旧版本不变
        raise HTTPException(502, {"message": f"生成检索向量失败，本次上传未生效（旧版本不变）：{type(e).__name__}: {str(e)[:200]}"}) from None
    return {"import_id": imp.id, "status": imp.status, "counts": preview["counts"], "diff": preview["diff"],
            "index": imp.summary.get("index")}


@router.get("/imports")
def list_imports(store_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(KnowledgeImport).where(KnowledgeImport.store_id == store_id)
                      .order_by(KnowledgeImport.id.desc()).limit(50))
    return [{"id": r.id, "file_name": r.file_name, "uploaded_by": r.uploaded_by, "uploaded_at": iso(r.uploaded_at),
             "status": r.status, "row_count": r.row_count, "public_count": r.public_count,
             "internal_count": r.internal_count, "summary": r.summary, "note": r.note} for r in rows]


class RollbackBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/imports/{import_id}/rollback", dependencies=[Depends(require_admin)])
def rollback_import(import_id: int, body: RollbackBody, reviewer: str = Depends(reviewer_name),
                    db: Session = Depends(get_db)):
    try:
        target = xi.rollback(db, import_id, reviewer, body.note)
    except vi.RagNotReady as e:
        raise HTTPException(409, {"message": "检索索引未就绪，回滚未执行", "reasons": e.reasons}) from None
    except LookupError as e:
        raise HTTPException(404, str(e)) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"import_id": target.id, "status": target.status}


# ---------------------------------------------------------------- 检索索引（第 15.3、15.4 节）
@router.get("/index/status")
def index_status(store_id: str, db: Session = Depends(get_db)):
    vi.vector_status(refresh=True)
    s = get_settings()
    return {**vi.index_status(db, store_id), "knowledge_mode": s.knowledge_mode}


class StoreBody(BaseModel):
    store_id: str


@router.post("/index/rebuild", dependencies=[Depends(require_admin)])
def rebuild_index(body: StoreBody, db: Session = Depends(get_db)):
    """补齐该店铺已发布条款的分词与向量（只对缺的调用向量模型）。"""
    chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.store_id.in_([body.store_id, "ALL"]),
                                                     KnowledgeChunk.review_state == "PUBLISHED")).all()
    try:
        result = vi.index_chunks(db, list(chunks))
    except vi.RagNotReady as e:
        raise HTTPException(409, {"message": "检索索引未就绪", "reasons": e.reasons}) from None
    except Exception as e:
        raise HTTPException(502, {"message": f"生成向量失败：{type(e).__name__}: {str(e)[:200]}"}) from None
    return {**result, "status": vi.index_status(db, body.store_id)}


class SearchBody(BaseModel):
    store_id: str
    marketplace_id: str
    query: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=30)


@router.post("/search")
def search(body: SearchBody, db: Session = Depends(get_db)):
    """检索测试：与 Agent 用同一套过滤与混合检索，返回每条候选的各路排名，便于调知识库。"""
    s = get_settings()
    store_sites(db, body.store_id)
    try:
        ctx = retrieve(db, body.store_id, body.marketplace_id, s.knowledge_date(), body.query,
                       top_k=body.top_k or s.knowledge_retrieval_top_k, candidates=s.knowledge_retrieval_candidates,
                       budget_tokens=s.knowledge_retrieval_max_tokens)
    except vi.RagNotReady as e:
        raise HTTPException(409, {"message": "检索索引未就绪", "reasons": e.reasons}) from None
    texts = {c.chunk_id: c.text for c in ctx.clauses}
    return {"query_tokens": ctx.query_tokens, "candidates": ctx.candidates, "tokens": ctx.tokens,
            "over_budget": ctx.over_budget, "hits": [{**h, "text": texts.get(h["chunk_id"])} for h in ctx.hits]}
