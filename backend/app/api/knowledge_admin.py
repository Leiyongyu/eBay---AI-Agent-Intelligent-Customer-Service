"""正式知识库维护：下载 Excel 模板、上传预览、整体替换、历史版本与回滚（第 5.4、12.3 节）。

上传的文件以 base64 放在 JSON 里传输（不额外依赖 multipart 解析库）。
提交前必须先预览：提交时带上预览返回的文件 SHA-256，服务端重新解析校验，确认是同一个文件且没有错误。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import date
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
ALL_STORES = "*"  # 上传、导出、清理时表示全部店铺


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


def _known_stores(db: Session) -> dict[str, str]:
    """“适用店铺”列可以填店铺名或店铺 ID（不区分大小写）。"""
    out: dict[str, str] = {}
    for sid, name in db.execute(select(Store.id, Store.name)).all():
        out[sid.lower()] = sid
        if name:
            out.setdefault(name.strip().lower(), sid)
    return out


def _targets(db: Session, store_id: str) -> list[tuple[str, str, list[str]]]:
    """上传目标：[(店铺 ID, 店铺名, 开通站点)]；store_id=* 为全部店铺。"""
    if store_id == ALL_STORES:
        rows = db.execute(select(Store.id, Store.name).order_by(Store.name)).all()
        return [(sid, name or sid, store_sites(db, sid)) for sid, name in rows]
    sites = store_sites(db, store_id)
    return [(store_id, (db.get(Store, store_id).name or store_id), sites)]


def _parse(db: Session, body: UploadBody):
    """解析并预览。返回（文件内容, [(店铺 ID, 店铺名, 该店铺的条款, 该店铺的预览)], 给页面的预览）。
    单个店铺：和以前一样，只是“适用店铺”不含它的行跳过；全部店铺：每个店铺按“适用店铺”列取自己的条款，
    文件里没有任何条款适用的店铺不动。"""
    data = _decode(body)
    targets = _targets(db, body.store_id)
    many = body.store_id == ALL_STORES
    union = sorted({x for _, _, sites in targets for x in sites})
    try:
        from ..knowledge.kb_docs import known_docs

        parsed = xi.parse_workbook(data, union, known_stores=_known_stores(db), known_docs=known_docs(db))
    except xi.ExcelImportError as e:
        raise HTTPException(422, {"message": str(e)}) from None
    budget = get_settings().knowledge_full_context_max_tokens
    per = []
    for sid, name, sites in targets:
        sub = xi.for_store(parsed, sid, sites, strict=not many)
        if many and not sub.rows:
            continue  # 文件里没有这个店铺的条款：不动它
        per.append((sid, name, sub, xi.preview(db, sid, sub, sites, budget)))
    sha = hashlib.sha256(data).hexdigest()
    if not many:
        preview = per[0][3]
        skipped = len(parsed.rows) - len(per[0][2].rows)
        if skipped and not per[0][2].errors:
            preview["warnings"].append({"row": None, "column": "适用店铺", "message": f"有 {skipped} 行不适用于这个店铺，已跳过"})
        preview.update(file_sha256=sha, sites=targets[0][2])
        return data, per, preview

    # 全部店铺：汇总
    base = {(e["row"], e["column"], e["message"]) for e in parsed.errors}
    errors = list(parsed.errors)
    warnings = list(parsed.warnings)
    base_w = {(w["row"], w["column"], w["message"]) for w in parsed.warnings}
    site_tokens: dict[str, int] = {}
    stores, added, changed, removed = [], set(), set(), set()
    for sid, name, sub, pv in per:
        errors += [{**e, "message": f"【{name}】{e['message']}"} for e in sub.errors
                   if (e["row"], e["column"], e["message"]) not in base]
        warnings += [{**w, "message": f"【{name}】{w['message']}"} for w in pv["warnings"]
                     if (w["row"], w["column"], w["message"]) not in base_w]
        for site, n in pv["site_tokens"].items():
            site_tokens[site] = max(site_tokens.get(site, 0), n)
        d = pv["diff"]
        added |= set(d["added"])
        changed |= set(d["changed"])
        removed |= set(d["removed"])
        stores.append({"store_id": sid, "name": name, "rows": len(sub.rows), "added": len(d["added"]),
                       "changed": len(d["changed"]), "removed": len(d["removed"]), "unchanged": d["unchanged"],
                       "current_import": pv["current_import"]})
    rows = parsed.rows
    preview = {
        "ok": not errors, "errors": errors, "warnings": warnings,
        "rows": [{"row": r.row_no, "clause_id": r.clause_id, "title": r.title, "sites": r.sites, "stores": r.stores,
                  "doc_refs": r.doc_refs,
                  "audience": r.audience, "topic": r.topic, "priority": r.priority, "effective_at": r.effective_at,
                  "expires_at": r.expires_at, "enabled": r.enabled, "chars": len(r.text), "scenes": r.scenes}
                 for r in rows],
        "counts": {"rows": len(rows), "public": sum(r.audience == "PUBLIC_REPLY" for r in rows),
                   "internal": sum(r.audience == "INTERNAL_SOP" for r in rows),
                   "disabled": sum(not r.enabled for r in rows), "stores": len(per),
                   "all_store_rows": sum(r.stores is None for r in rows)},
        "site_tokens": site_tokens, "budget_tokens": budget,
        # 各店铺差异的合集（某个编号在任一店铺新增 / 修改 / 删除）；不变 = 每个店铺都没变的店铺数
        "diff": {"added": sorted(added), "changed": sorted(changed), "removed": sorted(removed),
                 "unchanged": sum(1 for x in stores if not (x["added"] or x["changed"] or x["removed"]))},
        "stores": stores, "untouched_stores": len(targets) - len(per),
        "current_import": None, "file_sha256": sha, "sites": sorted({x for _, _, sites in targets for x in sites}),
    }
    return data, per, preview


@router.post("/upload/preview", dependencies=[Depends(require_admin)])
def upload_preview(body: UploadBody, db: Session = Depends(get_db)):
    """只解析校验，不写库。"""
    return _parse(db, body)[2]


@router.post("/upload/commit", dependencies=[Depends(require_admin)])
def upload_commit(body: UploadBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    data, per, preview = _parse(db, body)
    if not body.expected_sha256 or body.expected_sha256 != preview["file_sha256"]:
        raise HTTPException(409, "文件与预览时不一致，请重新预览后再提交")
    if preview["errors"]:
        raise HTTPException(422, {"message": "还有错误未修正", "errors": preview["errors"]})
    imps = []
    try:
        # 全部店铺时每个店铺各生成一个版本；同一个事务里，任何一个失败整体不生效
        for sid, _name, sub, pv in per:
            summary = {k: pv[k] for k in ("counts", "site_tokens", "budget_tokens", "diff", "warnings")}
            if body.store_id == ALL_STORES:
                summary["all_stores_upload"] = True
            imps.append(xi.commit(db, sid, sub, body.file_name, data, reviewer, summary))
    except vi.RagNotReady as e:
        raise HTTPException(409, {"message": "检索索引未就绪，本次上传未生效（旧版本不变）", "reasons": e.reasons}) from None
    except Exception as e:  # 向量模型调用失败等：事务回滚，旧版本不变
        raise HTTPException(502, {"message": f"生成检索向量失败，本次上传未生效（旧版本不变）：{type(e).__name__}: {str(e)[:200]}"}) from None
    imp = imps[0]
    return {"import_id": imp.id, "status": imp.status, "counts": preview["counts"], "diff": preview["diff"],
            "index": imp.summary.get("index"), "stores": len(imps), "import_ids": [i.id for i in imps]}


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


# ---------------------------------------------------------------- 导出与清理（管理员）


def _scope_stores(db: Session, store_id: str) -> list[tuple[str, str]]:
    """store_id 为 * 时是全部店铺（含已下线但还有知识的店铺 ID）。"""
    names = dict(db.execute(select(Store.id, Store.name)).all())
    if store_id == ALL_STORES:
        ids = set(names) | set(db.scalars(select(KnowledgeChunk.store_id).distinct()))
        return sorted(((i, names.get(i) or i) for i in ids), key=lambda x: x[1])
    store_sites(db, store_id)  # 店铺不存在时 404
    return [(store_id, names.get(store_id) or store_id)]


@router.get("/export", dependencies=[Depends(require_admin)])
def export_knowledge(store_id: str, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """导出当前知识为一个 Excel（上传模板格式 + “适用店铺”列，可改完直接再上传）；store_id=* 为全部店铺，
    各店铺相同的条款合并成一行。"""
    from ..knowledge import maintenance as km
    from ..models import AuditEvent

    stores = _scope_stores(db, store_id)
    today = date.today().isoformat()
    data, n = km.export_workbook(db, stores, all_stores=store_id == ALL_STORES)
    label = "全部店铺" if store_id == ALL_STORES else km.safe_name(stores[0][1])
    name = f"知识库_{label}_{today}.xlsx"
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    detail = {"rows": n, "stores": len(stores)}
    db.add(AuditEvent(actor=reviewer, action="KNOWLEDGE_EXPORTED", store_id=None if store_id == ALL_STORES else store_id,
                      detail=detail))
    return Response(data, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.get("/cleanup/preview", dependencies=[Depends(require_admin)])
def cleanup_preview(store_id: str, db: Session = Depends(get_db)):
    from ..knowledge import maintenance as km

    return km.stats(db, [s for s, _ in _scope_stores(db, store_id)])


class CleanupBody(BaseModel):
    store_id: str
    mode: str = Field(pattern="^(history|all)$")
    confirm: str | None = None  # 清空（all）时必须填“清空”


@router.post("/cleanup", dependencies=[Depends(require_admin)])
def cleanup(body: CleanupBody, reviewer: str = Depends(reviewer_name), db: Session = Depends(get_db)):
    """history：只删历史版本（当前生效的不动）；all：清空店铺全部知识（AI 没有知识依据，直到重新上传）。不能恢复。"""
    from ..knowledge import maintenance as km

    if body.mode == "all" and (body.confirm or "").strip() != "清空":
        raise HTTPException(422, "清空知识库需要在确认框里输入“清空”")
    return km.cleanup(db, [s for s, _ in _scope_stores(db, body.store_id)], body.mode, reviewer)


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
