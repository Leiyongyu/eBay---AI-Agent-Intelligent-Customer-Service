"""参考文档库：第三方文件和内部资料（报表、手册、供应商资料、物流手册），知识条款用编号（DOC-0001）引用。

- 只给人看：文档不进提示词、不建向量、不发给买家（知识由人整理后上传，见方案 v2.2 第 21.2 节）；
- 文件存在 KB_DOCS_DIR/<编号>/v<版本>_<安全文件名>，替换新版本时旧文件保留；
- 权限：管理员上传、改、替换、归档、删除；其他账号只看自己负责店铺的文档（适用店铺为空 = 全部店铺），
  敏感文档非管理员只能看到标题、不能下载；
- 被当前条款（已发布 / 未启用）引用的文档不能删；清空知识库不删文档；不受聊天记录 30 天保留影响。
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import PROJECT_ROOT, get_settings
from ..db import utcnow
from ..models import AuditEvent, KbDocument, KbDocumentVersion, KnowledgeChunk

ALLOWED_EXT = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
INLINE_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md"}  # 浏览器里直接预览，其他下载
CATEGORIES = ["报表", "手册", "供应商资料", "物流", "其他"]
DOC_NO = re.compile(r"\bDOC-\d{4}\b")
CURRENT_STATES = ("PUBLISHED", "DRAFT")


class KbDocError(ValueError):
    def __init__(self, status: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.detail = status, message, detail


# ---------------------------------------------------------------- 文件
def safe_file_name(name: str) -> str:
    """去掉路径（../、盘符、反斜杠）和不安全字符，保留中文、字母数字和 . - _ 空格。"""
    base = re.split(r"[\\/]", name or "")[-1].strip().strip(".")
    base = re.sub(r"[^\w.\- ]+", "_", base)[:120].strip()
    return base or "file"


def ext_of(name: str) -> str:
    return Path(name).suffix.lower()


def check_file(name: str, data: bytes) -> None:
    ext = ext_of(name)
    if ext not in ALLOWED_EXT:
        raise KbDocError(422, f"不支持的文件类型 {ext or '（没有扩展名）'}，只能上传 " + "、".join(sorted(x[1:] for x in ALLOWED_EXT)))
    if not data:
        raise KbDocError(422, "文件是空的")
    limit = get_settings().kb_docs_max_mb
    if len(data) > limit * 1024 * 1024:
        raise KbDocError(422, f"文件超过 {limit} MB")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:  # 存放目录不在项目里（如测试的临时目录）
        return str(path)


def abs_path(stored: str) -> Path:
    p = Path(stored)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _save(doc_no: str, version: int, name: str, data: bytes) -> dict:
    folder = get_settings().kb_docs_root / doc_no
    folder.mkdir(parents=True, exist_ok=True)
    safe = safe_file_name(name)
    path = folder / f"v{version}_{safe}"
    path.write_bytes(data)
    from ..aftersales_mail.storage import scan_file

    status, note = scan_file(path)  # 本机 Defender 服务停着时记 SKIPPED（未扫描），不改系统安全设置
    if status == "THREAT":
        raise KbDocError(422, note or "病毒扫描发现威胁，文件已删除")
    ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
    return {"file_name": safe, "content_type": ctype, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "stored_path": _rel(path), "scan_status": status}


def next_doc_no(db: Session) -> str:
    """按历史最大编号加 1（删掉的编号不复用：审计里记过的编号也算）。"""
    nums = [int(x[4:]) for x in db.scalars(select(KbDocument.doc_no)) if DOC_NO.fullmatch(x or "")]
    for d in db.scalars(select(AuditEvent.detail).where(AuditEvent.action == "KB_DOC_DELETED")):
        no = (d or {}).get("doc_no") or ""
        if DOC_NO.fullmatch(no):
            nums.append(int(no[4:]))
    return f"DOC-{(max(nums) if nums else 0) + 1:04d}"


# ---------------------------------------------------------------- 权限
def can_see(user, doc: KbDocument) -> bool:
    """None = 后台任务 / 脚本，不限制。"""
    if user is None or user.role == "ADMIN" or user.sees_all_stores or not doc.store_ids:
        return True
    return bool(set(doc.store_ids) & set(user.store_ids))


def can_download(user, doc: KbDocument) -> bool:
    if not can_see(user, doc):
        return False
    return not doc.sensitive or user is None or user.role == "ADMIN"


def file_url(doc: KbDocument, version: int | None = None) -> str:
    return f"/internal/kb-docs/{doc.id}/file" + (f"?version={version}" if version else "")


# ---------------------------------------------------------------- 引用
def refs_of(db: Session, doc_nos: list[str] | None = None) -> dict[str, list[dict]]:
    """当前条款（已发布 / 未启用）对文档的引用：{编号: [{store_id, clause_id, title}]}（同店同条款只记一次）。"""
    out: dict[str, list[dict]] = {}
    seen = set()
    rows = db.execute(select(KnowledgeChunk.store_id, KnowledgeChunk.clause_id, KnowledgeChunk.title, KnowledgeChunk.doc_refs)
                      .where(KnowledgeChunk.review_state.in_(CURRENT_STATES), KnowledgeChunk.doc_refs.is_not(None))).all()
    for sid, cid, title, refs in rows:
        for no in refs or []:
            if doc_nos is not None and no not in doc_nos:
                continue
            if (no, sid, cid) in seen:
                continue
            seen.add((no, sid, cid))
            out.setdefault(no, []).append({"store_id": sid, "clause_id": cid, "title": title})
    return out


def docs_brief(db: Session, doc_nos: list[str], user) -> list[dict]:
    """条款旁显示的文档链接：[{doc_no, title, file_url | None, missing}]；看不到的店铺文档只给编号，敏感文档非管理员没有链接。"""
    if not doc_nos:
        return []
    docs = {d.doc_no: d for d in db.scalars(select(KbDocument).where(KbDocument.doc_no.in_(doc_nos)))}
    out = []
    for no in doc_nos:
        d = docs.get(no)
        if d is None:
            out.append({"doc_no": no, "title": None, "file_url": None, "missing": True})
        elif not can_see(user, d):
            out.append({"doc_no": no, "title": None, "file_url": None, "missing": False})
        else:
            out.append({"doc_no": no, "title": d.title, "file_url": file_url(d) if can_download(user, d) else None,
                        "missing": False, "sensitive": d.sensitive, "archived": d.status == "ARCHIVED"})
    return out


def known_docs(db: Session) -> dict[str, dict]:
    """知识库 Excel 校验用：{编号: {status, sensitive, store_ids, title}}。"""
    return {d.doc_no: {"status": d.status, "sensitive": d.sensitive, "store_ids": d.store_ids, "title": d.title}
            for d in db.scalars(select(KbDocument))}


# ---------------------------------------------------------------- 增改删
def _meta(meta: dict) -> dict:
    title = (meta.get("title") or "").strip()
    if not title:
        raise KbDocError(422, "请填写标题")
    category = (meta.get("category") or "其他").strip()
    if category not in CATEGORIES:
        raise KbDocError(422, "分类只能是：" + "、".join(CATEGORIES))
    stores = [s for s in (meta.get("store_ids") or []) if s] or None
    return {"title": title[:200], "description": (meta.get("description") or "").strip() or None, "category": category,
            "store_ids": stores, "sensitive": bool(meta.get("sensitive"))}


def create(db: Session, meta: dict, name: str, data: bytes, actor: str) -> KbDocument:
    check_file(name, data)
    m = _meta(meta)
    no = next_doc_no(db)
    f = _save(no, 1, name, data)
    doc = KbDocument(doc_no=no, version=1, status="ACTIVE", uploaded_by=actor[:64], uploaded_at=utcnow(),
                     updated_by=actor[:64], **m, **f)
    db.add(doc)
    db.flush()
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_UPLOADED",
                      detail={"doc_id": doc.id, "doc_no": no, "title": doc.title, "file": doc.file_name, "size": doc.size,
                              "scan": doc.scan_status}))
    return doc


def update(db: Session, doc: KbDocument, meta: dict, actor: str) -> KbDocument:
    for k, v in _meta(meta).items():
        setattr(doc, k, v)
    doc.updated_by = actor[:64]
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_UPDATED", detail={"doc_id": doc.id, "doc_no": doc.doc_no}))
    db.flush()
    return doc


def replace(db: Session, doc: KbDocument, name: str, data: bytes, actor: str) -> KbDocument:
    check_file(name, data)
    db.add(KbDocumentVersion(doc_id=doc.id, version=doc.version, file_name=doc.file_name, content_type=doc.content_type,
                             size=doc.size, sha256=doc.sha256, stored_path=doc.stored_path,
                             uploaded_by=doc.updated_by or doc.uploaded_by, uploaded_at=doc.updated_at or doc.uploaded_at))
    f = _save(doc.doc_no, doc.version + 1, name, data)
    for k, v in f.items():
        setattr(doc, k, v)
    doc.version += 1
    doc.updated_by = actor[:64]
    doc.updated_at = utcnow()
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_REPLACED",
                      detail={"doc_id": doc.id, "doc_no": doc.doc_no, "version": doc.version, "file": doc.file_name}))
    db.flush()
    return doc


def set_status(db: Session, doc: KbDocument, archived: bool, actor: str) -> KbDocument:
    doc.status = "ARCHIVED" if archived else "ACTIVE"
    doc.updated_by = actor[:64]
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_ARCHIVED" if archived else "KB_DOC_RESTORED",
                      detail={"doc_id": doc.id, "doc_no": doc.doc_no}))
    db.flush()
    return doc


def delete(db: Session, doc: KbDocument, actor: str) -> dict:
    refs = refs_of(db, [doc.doc_no]).get(doc.doc_no) or []
    if refs:
        raise KbDocError(409, f"{doc.doc_no} 还被 {len(refs)} 条当前条款引用，不能删除；请先在知识库 Excel 里去掉引用再上传",
                         {"refs": refs})
    versions = db.scalars(select(KbDocumentVersion).where(KbDocumentVersion.doc_id == doc.id)).all()
    removed = 0
    for p in [doc.stored_path, *[v.stored_path for v in versions]]:
        f = abs_path(p)
        try:
            f.resolve().relative_to(get_settings().kb_docs_root.resolve())  # 只删文档目录里的文件
        except ValueError:
            continue
        if f.is_file():
            f.unlink()
            removed += 1
    folder = get_settings().kb_docs_root / doc.doc_no
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    for v in versions:
        db.delete(v)
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_DELETED",
                      detail={"doc_id": doc.id, "doc_no": doc.doc_no, "title": doc.title, "files": removed}))
    db.delete(doc)
    db.flush()
    return {"doc_no": doc.doc_no, "deleted_files": removed}


def log_download(db: Session, doc: KbDocument, version: int, actor: str) -> None:
    """同一个人同一版本 10 分钟内重复打开只记一次。"""
    since = utcnow() - timedelta(minutes=10)
    recent = db.scalars(select(AuditEvent.detail).where(
        AuditEvent.action == "KB_DOC_DOWNLOADED", AuditEvent.actor == actor[:64], AuditEvent.created_at >= since)).all()
    if any((d or {}).get("doc_id") == doc.id and (d or {}).get("version") == version for d in recent):
        return
    db.add(AuditEvent(actor=actor[:64], action="KB_DOC_DOWNLOADED",
                      detail={"doc_id": doc.id, "doc_no": doc.doc_no, "version": version}))


def ref_counts(db: Session) -> dict[str, int]:
    return {no: len(v) for no, v in refs_of(db).items()}


def count(db: Session) -> int:
    return db.scalar(select(func.count(KbDocument.id))) or 0
