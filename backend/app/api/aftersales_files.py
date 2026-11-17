"""售后邮件附件接口。

权限：管理员，或账号管理里打开了“可看售后附件”的账号，且只能看自己负责店铺的；“待认领”（还没对上订单的）只有管理员和
负责全部店铺的账号能看。查看、下载、认领都记审计。文件只能登录后经这里访问，不对公网开放。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..aftersales_mail import storage
from ..aftersales_mail.service import ClaimError, claim, worker
from ..auth import CurrentUser, current_user
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import (
    AftersalesFile,
    AftersalesMail,
    AsFileStatus,
    AuditEvent,
    BuyerUpload,
    BuyerUploadStatus,
    Conversation,
    Role,
)
from ..upload_page import service as upload_svc
from .deps import iso

router = APIRouter(prefix="/internal", tags=["aftersales-files"])


def allowed(user: CurrentUser) -> bool:
    """管理员始终可看；其他账号要管理员在“账号管理”里打开“可看售后附件”。"""
    return user.is_admin or user.can_aftersales_files


def _user(request: Request, db: Session) -> CurrentUser:
    user = current_user(request)
    if not allowed(user):
        raise HTTPException(403, "没有“售后附件”权限，请联系管理员在账号管理里开通")
    return user


def _can_see(user: CurrentUser, store_id: str | None) -> bool:
    return user.sees_all_stores if store_id is None else user.can_see_store(store_id)


def _check(user: CurrentUser, store_id: str | None) -> None:
    if not _can_see(user, store_id):
        raise HTTPException(404, "不存在或不在你负责的店铺")


def _days_left(f: AftersalesFile) -> int | None:
    if f.status != AsFileStatus.STORED or not f.received_at:
        return None
    s = get_settings()
    days = max(s.logistics_pod_retention_days, s.aftersales_retention_days) if f.source == "GOODCANG_TICKET" else s.aftersales_retention_days
    left = f.received_at + timedelta(days=days) - utcnow()
    return max(0, left.days + (1 if left.seconds else 0))


def _kind(f: AftersalesFile) -> str:
    ext = storage.ext_of(f.original_name or f.rel_path, f.content_type)
    if ext in storage.PREVIEW_IMAGE:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in storage.PLAYABLE_VIDEO:
        return "video"
    return "download"


def _file_dict(f: AftersalesFile) -> dict:
    return {"id": f.id, "mail_id": f.mail_id, "store_id": f.store_id, "order_id": f.order_id,
            "conversation_id": f.conversation_id, "source": f.source, "original_name": f.original_name,
            "content_type": f.content_type, "size_bytes": f.size_bytes,
            "file_name": f.rel_path.rsplit("/", 1)[-1] if f.rel_path else None, "link_url": f.link_url,
            "status": f.status, "status_label": AsFileStatus.LABELS.get(f.status, f.status),
            "status_reason": f.status_reason, "scan_result": f.scan_result, "received_at": iso(f.received_at),
            "purged_at": iso(f.purged_at), "days_left": _days_left(f), "kind": _kind(f),
            "claimed_by": f.claimed_by}


def _mail_dict(m: AftersalesMail, files: list[AftersalesFile]) -> dict:
    return {"kind": "mail", "id": m.id, "subject": m.subject, "sender": m.sender, "received_at": iso(m.received_at),
            "processed_at": iso(m.processed_at), "order_id": m.order_id, "store_id": m.store_id,
            "conversation_id": m.conversation_id, "match": m.match, "status": m.status, "files_saved": m.files,
            "body_excerpt": m.body_excerpt, "files": [_file_dict(f) for f in files]}


def _with_files(db: Session, mails: list[AftersalesMail]) -> list[dict]:
    ids = [m.id for m in mails]
    files: dict[int, list] = {i: [] for i in ids}
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.mail_id.in_(ids)).order_by(AftersalesFile.id)):
        files[f.mail_id].append(f)
    return [_mail_dict(m, files[m.id]) for m in mails]


def _uploads(db: Session, uploads: list[BuyerUpload]) -> list[dict]:
    ids = [u.id for u in uploads]
    files: dict[int, list] = {i: [] for i in ids}
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.upload_id.in_(ids)).order_by(AftersalesFile.id)):
        files[f.upload_id].append(_file_dict(f))
    return [upload_svc.upload_dict(db, u, files[u.id]) for u in uploads]


def _upload_query(user: CurrentUser, store_id, q, date_from, date_to):
    """买家已提交的上传（草稿不算），按负责的店铺。"""
    query = select(BuyerUpload).where(BuyerUpload.status.in_((BuyerUploadStatus.SUBMITTED, BuyerUploadStatus.RECEIVED))) \
        .order_by(BuyerUpload.submitted_at.desc())
    if not user.sees_all_stores:
        query = query.where(BuyerUpload.store_id.in_(user.store_ids or ["-"]))
    if store_id:
        query = query.where(BuyerUpload.store_id == store_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(BuyerUpload.order_id.ilike(like), BuyerUpload.code.ilike(like),
                                BuyerUpload.buyer_key.ilike(like)))
    if date_from:
        query = query.where(BuyerUpload.submitted_at >= date_from)
    if date_to:
        query = query.where(BuyerUpload.submitted_at < f"{date_to} 23:59:59.999")
    return query


@router.get("/aftersales-files")
def list_mails(request: Request, store_id: str | None = None, q: str | None = None, date_from: str | None = None,
               date_to: str | None = None, unclaimed: bool = False, source: str | None = None, limit: int = 50,
               offset: int = 0, db: Session = Depends(get_db)):
    """按邮件 / 上传列出（每条带它的文件），新的在前。unclaimed=true 只看待认领的邮件；
    source：mail 只看售后邮箱 / upload 只看上传页 / 空为全部。"""
    user = _user(request, db)
    limit, offset = min(max(limit, 1), 200), max(offset, 0)
    if source == "upload" and not unclaimed:
        uq = _upload_query(user, store_id, q, date_from, date_to)
        total = db.scalar(select(func.count()).select_from(uq.order_by(None).subquery())) or 0
        return {"total": total, "items": _uploads(db, list(db.scalars(uq.limit(limit).offset(offset)))),
                "unclaimed": None, "retention_days": get_settings().aftersales_retention_days}
    query = select(AftersalesMail).order_by(AftersalesMail.id.desc())
    if unclaimed:
        if not user.sees_all_stores:
            raise HTTPException(403, "待认领的文件只有管理员和负责全部店铺的账号能看")
        query = query.where(AftersalesMail.store_id.is_(None))
    else:
        query = query.where(AftersalesMail.store_id.is_not(None))
        if not user.sees_all_stores:
            query = query.where(AftersalesMail.store_id.in_(user.store_ids or ["-"]))
    if store_id:
        query = query.where(AftersalesMail.store_id == store_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(AftersalesMail.order_id.ilike(like), AftersalesMail.subject.ilike(like),
                                AftersalesMail.sender.ilike(like)))
    if date_from:
        query = query.where(AftersalesMail.processed_at >= date_from)
    if date_to:
        query = query.where(AftersalesMail.processed_at < f"{date_to} 23:59:59.999")
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    unclaimed_n = db.scalar(select(func.count()).select_from(AftersalesMail).where(
        AftersalesMail.store_id.is_(None))) if user.sees_all_stores else None
    if source == "mail" or unclaimed:
        mails = db.scalars(query.limit(limit).offset(offset)).all()
        return {"total": total, "items": _with_files(db, list(mails)), "unclaimed": unclaimed_n,
                "retention_days": get_settings().aftersales_retention_days}
    # 全部：邮件和上传按时间合并
    uq = _upload_query(user, store_id, q, date_from, date_to)
    total += db.scalar(select(func.count()).select_from(uq.order_by(None).subquery())) or 0
    rows = _with_files(db, list(db.scalars(query.limit(limit + offset)))) + \
        _uploads(db, list(db.scalars(uq.limit(limit + offset))))
    rows.sort(key=lambda r: r.get("submitted_at") or r.get("processed_at") or "", reverse=True)
    return {"total": total, "items": rows[offset:offset + limit], "unclaimed": unclaimed_n,
            "retention_days": get_settings().aftersales_retention_days}


@router.get("/conversations/{conv_id}/aftersales-files")
def conversation_files(conv_id: int, request: Request, db: Session = Depends(get_db)):
    """会话里的售后附件区。没有权限时返回 allowed=false（前端显示提示，不报错）。"""
    user = current_user(request)
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    _check(user, conv.store_id)
    s = get_settings()
    links = [{"id": u.id, "order_id": u.order_id, "status": u.status,
              "status_label": BuyerUploadStatus.LABELS.get(u.status, u.status), "created_by": u.created_by,
              "expires_at": iso(u.link_expires_at)}
             for u in db.scalars(select(BuyerUpload).where(BuyerUpload.conversation_id == conv.id,
                                                           BuyerUpload.verify_method == "LINK",
                                                           BuyerUpload.status == BuyerUploadStatus.DRAFT)
                                 .order_by(BuyerUpload.id.desc()).limit(5))]
    base = {"upload_ready": upload_svc.ready(), "upload_not_ready_reasons": s.upload_not_ready_reasons(),
            "pending_links": links}
    if not allowed(user):
        return {"allowed": False, "items": [], "uploads": [], **base}
    mails = db.scalars(select(AftersalesMail).where(AftersalesMail.conversation_id == conv.id)
                       .order_by(AftersalesMail.id.desc())).all()
    uploads = db.scalars(select(BuyerUpload).where(
        BuyerUpload.conversation_id == conv.id,
        BuyerUpload.status.in_((BuyerUploadStatus.SUBMITTED, BuyerUploadStatus.RECEIVED)))
        .order_by(BuyerUpload.id.desc())).all()
    return {"allowed": True, "items": _with_files(db, list(mails)), "uploads": _uploads(db, list(uploads)),
            "retention_days": s.aftersales_retention_days, "mailbox": s.aftersales_public_email, **base}


class LinkBody(BaseModel):
    order_id: str = Field(min_length=5, max_length=32)


@router.post("/conversations/{conv_id}/upload-link")
def upload_link(conv_id: int, body: LinkBody, request: Request, db: Session = Depends(get_db)):
    """客服生成上传链接（贴进发给买家的邮件；链接不能放进 eBay 消息）。买家点开不用再校验订单。"""
    user = current_user(request)
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    _check(user, conv.store_id)
    if not upload_svc.ready():
        raise HTTPException(409, "上传页还没开通：" + "；".join(get_settings().upload_not_ready_reasons()))
    try:
        up, url = upload_svc.create_link(db, conv=conv, order_id=body.order_id, actor=user.display_name)
    except upload_svc.UploadError as e:
        raise HTTPException(e.status_code, e.message) from None
    return {"url": url, "upload_id": up.id, "order_id": up.order_id, "expires_at": iso(up.link_expires_at)}


@router.get("/aftersales-files/{file_id}/content")
def file_content(file_id: int, request: Request, download: bool = False, db: Session = Depends(get_db)):
    """预览（图片、PDF、视频支持拖动进度）或下载。"""
    user = _user(request, db)
    f = db.get(AftersalesFile, file_id)
    if f is None:
        raise HTTPException(404, "文件不存在")
    _check(user, f.store_id)
    if f.status != AsFileStatus.STORED or not f.rel_path:
        raise HTTPException(410, AsFileStatus.LABELS.get(f.status, "文件不可用"))
    path = get_settings().aftersales_root / f.rel_path
    if not path.exists():
        raise HTTPException(410, "文件已不在磁盘上")
    rng = request.headers.get("range") or ""
    if not rng or rng.startswith("bytes=0-"):  # 视频拖动会发很多分段请求，只在开头记一次
        db.add(AuditEvent(actor=user.display_name, action="AFTERSALES_FILE_DOWNLOADED" if download
                          else "AFTERSALES_FILE_VIEWED", store_id=f.store_id, conversation_id=f.conversation_id,
                          detail={"file_id": f.id, "order_id": f.order_id}))
    return FileResponse(path, media_type=f.content_type or "application/octet-stream",
                        filename=f.original_name or path.name,
                        content_disposition_type="attachment" if download else "inline")


class ClaimBody(BaseModel):
    order_id: str = Field(min_length=5, max_length=64)


@router.post("/aftersales-mails/{mail_id}/claim")
def claim_mail(mail_id: int, body: ClaimBody, request: Request, db: Session = Depends(get_db)):
    """认领（或改关联）：填订单号，系统查到订单所属店铺后把文件挪过去并改名。"""
    user = _user(request, db)
    mail = db.get(AftersalesMail, mail_id)
    if mail is None:
        raise HTTPException(404, "邮件不存在")
    _check(user, mail.store_id)
    try:
        mail = claim(db, mail, body.order_id, user.display_name, lambda sid: _can_see(user, sid))
    except ClaimError as e:
        raise HTTPException(e.status_code, e.message) from None
    return _with_files(db, [mail])[0]


@router.get("/aftersales-files/status")
def status(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    s = get_settings()
    stored = db.execute(select(func.count(), func.coalesce(func.sum(AftersalesFile.size_bytes), 0)).where(
        AftersalesFile.status == AsFileStatus.STORED)).one()
    out = {"enabled": not s.aftersales_mail_not_ready_reasons(), "not_ready_reasons": s.aftersales_mail_not_ready_reasons(),
           "retention_days": s.aftersales_retention_days, "max_mb": s.aftersales_file_max_mb,
           "files": stored[0], "bytes": int(stored[1]), "cleanup_time": s.aftersales_cleanup_time,
           "share_with_buyers": bool(s.aftersales_public_email),
           "upload_ready": upload_svc.ready(), "upload_not_ready_reasons": s.upload_not_ready_reasons()}
    if user.role == Role.ADMIN:
        out.update({"user": s.aftersales_mail_user, "folder": s.aftersales_mail_folder,
                    "done_folder": s.aftersales_mail_done_folder, "dir": str(s.aftersales_root),
                    "worker_running": worker.running, "last_poll": worker.last_poll,
                    "last_poll_at": iso(worker.last_poll_at), "last_cleanup": worker.last_cleanup,
                    "last_pull": worker.last_pull})
    return out


@router.post("/aftersales-files/poll")
def poll_now(request: Request, db: Session = Depends(get_db)):
    """管理员：马上收一次邮件（不等定时）。"""
    if current_user(request).role != Role.ADMIN:
        raise HTTPException(403, "只有管理员能手动收信")
    from ..aftersales_mail.service import run_once

    return run_once()


@router.post("/aftersales-files/cleanup")
def cleanup_now(request: Request, db: Session = Depends(get_db)):
    """管理员：马上执行一次保留期清理。"""
    if current_user(request).role != Role.ADMIN:
        raise HTTPException(403, "只有管理员能手动清理")
    return worker.run_cleanup()

