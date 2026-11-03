"""买家上传页：订单校验 → 填问题 → 分块直传 R2 → 提交 → 后台拉回本机售后附件目录（复用售后邮箱那一套存储、扫描、权限、清理）。

- 订单校验：订单号 + 收货邮编或 eBay 用户名（二选一），或者客服生成的签名链接；通过后发一个会话令牌（库里只存哈希）。
- 文件：单个最多 AFTERSALES_FILE_MAX_MB，类型同售后邮箱，每个订单总共最多 UPLOAD_ORDER_MAX_GB。
- 拉回：从 R2 读回（断了从已下载的位置续），按“店铺 / 订单号_时间_序号”存放，Defender 扫描，删 R2 里的文件，
  关联会话、标“待售后”，备注写买家填的问题。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..aftersales_mail import storage
from ..aftersales_mail.service import _conversation, _folder, lookup_order_api
from ..config import get_settings
from ..db import session_scope, utcnow
from ..models import (
    AftersalesFile,
    Aftersales,
    AsFileStatus,
    AuditEvent,
    BuyerUpload,
    BuyerUploadStatus,
    Conversation,
    ConversationEvent,
    Store,
)
from . import guard
from .r2 import R2Error, r2

log = logging.getLogger(__name__)
ORDER_RE = re.compile(r"^\d{2}-\d{5}-\d{5}$")
ISSUES = {  # 问题类型 → 工作台“转售后”的类型
    "NOT_FIT": "不适配", "INSTALL": "其他", "DAMAGED": "破损/错发/缺件", "NOT_WORKING": "质量问题",
    "WRONG_MISSING": "破损/错发/缺件", "OTHER": "其他"}
ISSUE_LABELS = {"NOT_FIT": "不适配", "INSTALL": "安装问题", "DAMAGED": "商品损坏", "NOT_WORKING": "不工作/故障",
                "WRONG_MISSING": "错发/缺件", "OTHER": "其他"}
INSTALLED = {"INSTALLED": "已安装", "TRIED": "试装过", "NOT": "未安装"}
VEHICLE_KEYS = ("make", "model", "year", "engine", "vin")
SOURCE = "UPLOAD"


class UploadError(Exception):
    def __init__(self, status: int, message: str, code: str = "error"):
        super().__init__(message)
        self.status_code, self.message, self.code = status, message, code


def ready() -> bool:
    return not get_settings().upload_not_ready_reasons()


def _order_items(order: dict) -> list[dict]:
    return [{"item_id": li.get("legacyItemId"), "title": li.get("title") or "", "quantity": li.get("quantity") or 1}
            for li in order.get("lineItems") or []]


def fetch_order(store_id: str, order_id: str) -> dict | None:
    """读订单（含收货地址，只用来核对邮编，不返回给买家）。测试会替换掉。"""
    from ..sources import SourceNotAvailable, get_ebay_source

    try:
        return get_ebay_source().invoice_order(store_id, order_id)
    except SourceNotAvailable:
        return None


def _norm(v: str | None) -> str:
    return re.sub(r"[\s\-]", "", v or "").upper()


def _new_session(up: BuyerUpload) -> str:
    token = secrets.token_urlsafe(32)
    up.session_hash = hashlib.sha256(token.encode()).hexdigest()
    up.session_expires_at = utcnow() + timedelta(hours=get_settings().upload_session_hours)
    return token


def session_upload(db: Session, token: str | None) -> BuyerUpload:
    if not token:
        raise UploadError(401, "会话已失效，请重新打开上传页", "session")
    up = db.scalar(select(BuyerUpload).where(BuyerUpload.session_hash == hashlib.sha256(token.encode()).hexdigest()))
    if up is None or not up.session_expires_at or up.session_expires_at < utcnow():
        raise UploadError(401, "会话已失效，请重新打开上传页", "session")
    return up


def _public(db: Session, up: BuyerUpload, token: str, items: list[dict]) -> dict:
    store = db.get(Store, up.store_id)
    files = db.scalars(select(AftersalesFile).where(AftersalesFile.upload_id == up.id)).all()
    return {"session": token, "order_id": up.order_id, "store_name": (store.name if store else None) or up.store_id,
            "items": items, "status": up.status, "code": up.code,
            "form": {"item_id": up.item_id, "issue_type": up.issue_type, "description": up.description,
                     "vehicle": up.vehicle, "installed": up.installed},
            "files": [file_public(f) for f in files],
            "limits": {"max_file_bytes": storage.max_bytes(), "max_order_bytes": order_limit(),
                       "max_files": get_settings().upload_max_files, "part_bytes": part_size(),
                       "allowed": sorted(storage.ALLOWED_EXT)}}


def order_limit() -> int:
    return int(get_settings().upload_order_max_gb * 1024 ** 3)


def part_size() -> int:
    return max(5, get_settings().upload_part_mb) * 1024 * 1024  # S3 规定除最后一块外每块至少 5MB


def file_public(f: AftersalesFile) -> dict:
    return {"id": f.id, "name": f.original_name, "size": f.size_bytes, "status": f.status,
            "uploaded": f.r2_state in ("UPLOADED", "FETCHED")}


# ------------------------------------------------------------------ ① 校验订单
def verify(db: Session, *, order_id: str | None, zip_code: str | None, username: str | None, link_token: str | None,
           language: str | None, ip: str | None) -> dict:
    if link_token:
        uid = guard.read_link(link_token)
        up = db.get(BuyerUpload, uid) if uid else None
        if up is None or up.status == BuyerUploadStatus.EXPIRED:
            raise UploadError(403, "链接无效或已过期，请联系卖家重新获取", "link")
        order = fetch_order(up.store_id, up.order_id) or {}
        up.language = language or up.language
        token = _new_session(up)
        db.flush()
        return _public(db, up, token, _order_items(order))

    order_id = (order_id or "").strip()
    if not ORDER_RE.match(order_id):
        raise UploadError(422, "订单号格式不对，例如 12-34567-89012", "order_format")
    if not guard.limiter.hit(f"verify-order:{order_id}", 10, 3600):
        raise UploadError(429, "尝试次数太多，请一小时后再试", "rate")
    found = lookup_order_api(order_id)
    order = fetch_order(found[0], order_id) if found else None
    ok, method = False, None
    if order:
        ship = (((order.get("fulfillmentStartInstructions") or [{}])[0].get("shippingStep") or {}).get("shipTo") or {})
        postcode = (ship.get("contactAddress") or {}).get("postalCode")
        buyer = ((order.get("buyer") or {}).get("username") or "")
        if zip_code and postcode and _norm(zip_code) == _norm(postcode):
            ok, method = True, "ZIP"
        elif username and buyer and username.strip().lower() == buyer.lower():
            ok, method = True, "USERNAME"
    if not ok:
        raise UploadError(403, "订单号和邮编（或 eBay 用户名）对不上", "mismatch")  # 不说是哪一项不对
    store_id = found[0]
    buyer = (order.get("buyer") or {}).get("username")
    up = BuyerUpload(order_id=order_id, store_id=store_id, buyer_key=buyer,
                     conversation_id=_conversation(db, store_id, buyer), verify_method=method,
                     language=language, ip_masked=guard.mask_ip(ip), status=BuyerUploadStatus.DRAFT)
    db.add(up)
    db.flush()
    token = _new_session(up)
    db.add(AuditEvent(actor="upload_page", action="UPLOAD_VERIFIED", store_id=store_id, conversation_id=up.conversation_id,
                      detail={"upload_id": up.id, "order_id": order_id, "method": method, "ip": up.ip_masked}))
    db.flush()
    return _public(db, up, token, _order_items(order))


def create_link(db: Session, *, conv: Conversation, order_id: str, actor: str) -> tuple[BuyerUpload, str]:
    """客服在会话里生成上传链接（订单要是这个会话买家的）。"""
    order_id = (order_id or "").strip()
    if not ORDER_RE.match(order_id):
        raise UploadError(422, "订单号格式不对，例如 12-34567-89012")
    order = fetch_order(conv.store_id, order_id)
    if not order:
        raise UploadError(404, f"本店铺没有订单 {order_id}")
    buyer = (order.get("buyer") or {}).get("username")
    if conv.channel_source == "ebay" and (buyer or "").lower() != (conv.buyer_key or "").lower():
        raise UploadError(403, "这笔订单不是本会话买家的")
    expires = utcnow() + timedelta(days=get_settings().upload_link_days)
    up = BuyerUpload(order_id=order_id, store_id=conv.store_id, buyer_key=buyer, conversation_id=conv.id,
                     verify_method="LINK", status=BuyerUploadStatus.DRAFT, link_expires_at=expires, created_by=actor[:64])
    db.add(up)
    db.flush()
    url = f"{get_settings().upload_public_url.rstrip('/')}/upload/?t={guard.sign_link(up.id, expires)}"
    db.add(AuditEvent(actor=actor[:64], action="UPLOAD_LINK_CREATED", store_id=conv.store_id, conversation_id=conv.id,
                      detail={"upload_id": up.id, "order_id": order_id, "expires": expires.isoformat(timespec="seconds")}))
    db.flush()
    return up, url


# ------------------------------------------------------------------ ② 填问题
def save_form(db: Session, up: BuyerUpload, body: dict) -> None:
    if up.status != BuyerUploadStatus.DRAFT:
        raise UploadError(409, "已经提交过了", "submitted")
    issue = body.get("issue_type")
    if issue not in ISSUES:
        raise UploadError(422, "请选择问题类型", "issue")
    installed = body.get("installed")
    if installed not in (None, "", *INSTALLED):
        raise UploadError(422, "安装情况不对", "installed")
    vehicle = {k: str(v).strip()[:64] for k, v in (body.get("vehicle") or {}).items() if k in VEHICLE_KEYS and str(v).strip()}
    up.issue_type, up.installed = issue, installed or None
    up.description = (body.get("description") or "").strip()[:4000] or None
    up.vehicle = vehicle or None
    up.item_id = (str(body.get("item_id") or "")[:64]) or None
    up.item_title = (str(body.get("item_title") or "")[:300]) or None
    db.flush()


# ------------------------------------------------------------------ ③ 文件：登记 → 分块签名 → 合并
def register_file(db: Session, up: BuyerUpload, *, name: str, size: int, content_type: str | None) -> dict:
    if up.status != BuyerUploadStatus.DRAFT:
        raise UploadError(409, "已经提交过了，不能再加文件", "submitted")
    name = os.path.basename((name or "").strip())[:200] or "file"
    ext = storage.ext_of(name, content_type)
    if not storage.allowed(ext):
        raise UploadError(422, f"这种文件类型不能上传（{ext or '未知'}）", "type")
    if size <= 0 or size > storage.max_bytes():
        raise UploadError(422, f"单个文件最大 {get_settings().aftersales_file_max_mb // 1024} GB", "too_large")
    n = db.scalar(select(func.count()).select_from(AftersalesFile).where(AftersalesFile.upload_id == up.id)) or 0
    if n >= get_settings().upload_max_files:
        raise UploadError(422, f"最多上传 {get_settings().upload_max_files} 个文件", "too_many")
    used = db.scalar(select(func.coalesce(func.sum(AftersalesFile.size_bytes), 0)).join(
        BuyerUpload, BuyerUpload.id == AftersalesFile.upload_id).where(
        BuyerUpload.order_id == up.order_id, AftersalesFile.source == SOURCE,
        AftersalesFile.status.not_in((AsFileStatus.BLOCKED_TYPE, AsFileStatus.DOWNLOAD_FAILED)))) or 0
    if used + size > order_limit():
        raise UploadError(422, f"这个订单最多上传 {get_settings().upload_order_max_gb:g} GB", "order_quota")
    key = f"{up.store_id}/{up.order_id}/{up.id}/{uuid.uuid4().hex}{ext}"
    ctype = content_type or "application/octet-stream"
    try:
        upload_id = r2().create_multipart(key, ctype)
    except R2Error as e:
        raise UploadError(502, f"云端存储暂时不可用：{e}", "r2") from None
    f = AftersalesFile(upload_id=up.id, store_id=up.store_id, order_id=up.order_id, conversation_id=up.conversation_id,
                       source=SOURCE, original_name=name, content_type=ctype[:100], size_bytes=size,
                       status=AsFileStatus.UPLOADING, r2_key=key, r2_upload_id=upload_id, r2_state="UPLOADING")
    db.add(f)
    db.flush()
    return {"file_id": f.id, "part_bytes": part_size(), "parts": math.ceil(size / part_size())}


def _file(db: Session, up: BuyerUpload, file_id: int) -> AftersalesFile:
    f = db.get(AftersalesFile, file_id)
    if f is None or f.upload_id != up.id:
        raise UploadError(404, "文件不存在", "file")
    return f


def sign_parts(db: Session, up: BuyerUpload, file_id: int, part_numbers: list[int]) -> dict:
    """签发一批分块上传地址（15 分钟有效），并返回已经传好的分块（续传用）。"""
    f = _file(db, up, file_id)
    if f.r2_state != "UPLOADING":
        raise UploadError(409, "这个文件已经传完了", "done")
    total = math.ceil((f.size_bytes or 0) / part_size())
    bad = [n for n in part_numbers if not 1 <= n <= total]
    if bad or len(part_numbers) > 50:
        raise UploadError(422, "分块编号不对", "parts")
    try:
        done = r2().list_parts(f.r2_key, f.r2_upload_id)
        urls = {n: r2().presign_part(f.r2_key, f.r2_upload_id, n, 900) for n in part_numbers}
    except R2Error as e:
        raise UploadError(502, f"云端存储暂时不可用：{e}", "r2") from None
    return {"urls": {str(k): v for k, v in urls.items()}, "uploaded": done, "parts": total}


def complete_file(db: Session, up: BuyerUpload, file_id: int, parts: list[dict]) -> dict:
    """合并分块，核对总大小与登记的一致。"""
    f = _file(db, up, file_id)
    if f.r2_state != "UPLOADING":
        return file_public(f)
    total = math.ceil((f.size_bytes or 0) / part_size())
    numbers = sorted(int(p["part_number"]) for p in parts)
    if numbers != list(range(1, total + 1)):
        raise UploadError(422, "还有分块没传完", "incomplete")
    try:
        r2().complete(f.r2_key, f.r2_upload_id, [{"part_number": int(p["part_number"]), "etag": str(p["etag"])} for p in parts])
        size = r2().size(f.r2_key)
    except R2Error as e:
        raise UploadError(502, f"合并文件失败：{e}", "r2") from None
    if size != f.size_bytes:
        try:
            r2().delete(f.r2_key)
        except R2Error:
            pass
        f.r2_state, f.status, f.status_reason = "ABORTED", AsFileStatus.DOWNLOAD_FAILED, "上传后大小和登记的不一致，已丢弃"
        db.flush()
        raise UploadError(422, "文件大小不一致，请重新上传这个文件", "size")
    f.r2_state, f.status = "UPLOADED", AsFileStatus.WAITING_FETCH
    db.flush()
    return file_public(f)


def remove_file(db: Session, up: BuyerUpload, file_id: int) -> None:
    f = _file(db, up, file_id)
    if up.status != BuyerUploadStatus.DRAFT:
        raise UploadError(409, "已经提交过了", "submitted")
    try:
        if f.r2_state == "UPLOADING":
            r2().abort(f.r2_key, f.r2_upload_id)
        elif f.r2_state == "UPLOADED":
            r2().delete(f.r2_key)
    except R2Error:
        pass
    db.delete(f)
    db.flush()


# ------------------------------------------------------------------ ④ 提交
def submit(db: Session, up: BuyerUpload) -> dict:
    if up.status != BuyerUploadStatus.DRAFT:
        return {"code": up.code, "status": up.status}
    if not up.issue_type:
        raise UploadError(422, "请先填写问题类型", "issue")
    files = db.scalars(select(AftersalesFile).where(AftersalesFile.upload_id == up.id)).all()
    if any(f.r2_state == "UPLOADING" for f in files):
        raise UploadError(409, "还有文件没传完", "uploading")
    if not any(f.r2_state == "UPLOADED" for f in files):
        raise UploadError(422, "请至少上传一个文件", "no_files")
    up.status, up.submitted_at = BuyerUploadStatus.SUBMITTED, utcnow()
    up.code = f"AS-{up.submitted_at:%Y%m%d}-{up.id:04d}"
    db.add(AuditEvent(actor="upload_page", action="UPLOAD_SUBMITTED", store_id=up.store_id, conversation_id=up.conversation_id,
                      detail={"upload_id": up.id, "code": up.code, "files": len(files)}))
    db.flush()
    return {"code": up.code, "status": up.status}


# ------------------------------------------------------------------ 后台：从 R2 拉回
def _incoming(f: AftersalesFile):
    p = get_settings().aftersales_root / ".incoming" / f"{f.id}.part"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def pull_file(db: Session, f: AftersalesFile, seq: int) -> None:
    """拉回一个文件：断点续传到 .incoming/<id>.part，完整后按规则命名存进店铺文件夹，扫描，删 R2 里的文件。"""
    tmp = _incoming(f)
    have = tmp.stat().st_size if tmp.exists() else 0
    if have < (f.size_bytes or 0):
        with open(tmp, "ab") as out:
            for chunk in r2().read(f.r2_key, start=have):
                out.write(chunk)
    if tmp.stat().st_size != f.size_bytes:
        tmp.unlink(missing_ok=True)
        raise R2Error("拉回的大小和上传的不一致，下次重新拉")
    h = hashlib.sha256()
    with open(tmp, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    root = get_settings().aftersales_root
    ext = storage.ext_of(f.original_name, f.content_type)
    f.rel_path = storage.move(tmp.relative_to(root).as_posix(), _folder(db, f.store_id),
                              storage.file_name(f.order_id, datetime.now(), seq, ext))
    f.sha256, f.received_at = h.hexdigest(), utcnow()
    result, note = storage.scan(f.rel_path)
    f.scan_result = result
    if result == "THREAT":
        f.status, f.status_reason = AsFileStatus.INFECTED, note
    else:
        f.status, f.status_reason = AsFileStatus.STORED, note
    try:
        r2().delete(f.r2_key)
    except R2Error:
        log.warning("删除 R2 文件失败，留给清理任务：%s", f.r2_key)
    f.r2_state, f.fetch_error = "FETCHED", None


def _received(db: Session, up: BuyerUpload, files: list[AftersalesFile]) -> None:
    up.status, up.received_at = BuyerUploadStatus.RECEIVED, utcnow()
    saved = sum(f.status == AsFileStatus.STORED for f in files)
    if up.conversation_id:
        conv = db.get(Conversation, up.conversation_id)
        if conv is not None:
            issue = ISSUE_LABELS.get(up.issue_type or "", "其他")
            db.add(ConversationEvent(store_id=conv.store_id, conversation_id=conv.id, event_type="BUYER_UPLOAD_RECEIVED",
                                     source="upload_page", detail=f"买家通过上传页提交 {up.code}：{issue}，{saved} 个文件"))
            if conv.aftersales_status in (None, Aftersales.DONE):  # 自动标“待售后”，备注写买家填的问题
                conv.aftersales_status, conv.aftersales_handled_by = Aftersales.PENDING, None
                conv.aftersales_type = ISSUES.get(up.issue_type or "", "其他")
                conv.aftersales_marked_by, conv.aftersales_marked_at = "上传页", utcnow()
            note = f"【买家上传 {up.code}】{issue}" + (f"：{up.description}" if up.description else "")
            conv.aftersales_note = ((conv.aftersales_note or "") + "\n" + note).strip()[:4000]
            conv.aftersales_updated_at = utcnow()
            from ..read_state import mark_unread

            mark_unread(db, conv, "upload_page")
    db.add(AuditEvent(actor="upload_page", action="UPLOAD_RECEIVED", store_id=up.store_id, conversation_id=up.conversation_id,
                      detail={"upload_id": up.id, "code": up.code, "files": len(files), "saved": saved}))


def pull_once(limit: int = 20) -> dict:
    """把已提交上传里传完的文件拉回本机；全部处理完的上传记录标“已收到”。"""
    if not ready():
        return {"skipped": True}
    done = failed = 0
    with session_scope() as db:
        ids = list(db.scalars(select(AftersalesFile.id).join(BuyerUpload, BuyerUpload.id == AftersalesFile.upload_id).where(
            BuyerUpload.status == BuyerUploadStatus.SUBMITTED, AftersalesFile.r2_state == "UPLOADED")
            .order_by(AftersalesFile.id).limit(limit)))
    for fid in ids:
        try:
            with session_scope() as db:
                f = db.get(AftersalesFile, fid)
                seq = (db.scalar(select(func.count()).select_from(AftersalesFile).where(
                    AftersalesFile.upload_id == f.upload_id, AftersalesFile.id <= f.id)) or 1)
                pull_file(db, f, seq)
            done += 1
        except Exception as e:  # noqa: BLE001 — 单个文件失败下轮再试
            failed += 1
            log.warning("拉回上传文件 %s 失败：%s", fid, e)
            with session_scope() as db:
                f = db.get(AftersalesFile, fid)
                if f is not None:
                    f.fetch_error = str(e)[:500]
    with session_scope() as db:
        for up in db.scalars(select(BuyerUpload).where(BuyerUpload.status == BuyerUploadStatus.SUBMITTED)):
            files = db.scalars(select(AftersalesFile).where(AftersalesFile.upload_id == up.id)).all()
            if files and all(f.r2_state in ("FETCHED", "ABORTED") for f in files):
                _received(db, up, list(files))
    return {"fetched": done, "failed": failed}


# ------------------------------------------------------------------ 清理（并进售后附件每天的清理）
def cleanup(db: Session, now: datetime) -> dict:
    s = get_settings()
    out = {"upload_abandoned": 0, "upload_r2_deleted": 0, "upload_expired": 0, "upload_r2_stale": 0}
    link_cut = now - timedelta(days=s.upload_link_days)
    keep_cut = now - timedelta(days=s.aftersales_retention_days)
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.source == SOURCE, AftersalesFile.r2_state == "UPLOADING",
                                                     AftersalesFile.received_at < link_cut)):
        try:
            r2().abort(f.r2_key, f.r2_upload_id)
        except R2Error:
            pass
        f.r2_state, f.status, f.status_reason = "ABORTED", AsFileStatus.DOWNLOAD_FAILED, "买家没传完，已放弃"
        out["upload_abandoned"] += 1
    for f in db.scalars(select(AftersalesFile).where(AftersalesFile.source == SOURCE, AftersalesFile.r2_state == "UPLOADED",
                                                     AftersalesFile.received_at < keep_cut)):
        try:
            r2().delete(f.r2_key)
        except R2Error:
            pass
        f.r2_state, f.status, f.purged_at = "ABORTED", AsFileStatus.PURGED, now
        f.status_reason = f"满 {s.aftersales_retention_days} 天没拉回，已清理"
        out["upload_r2_deleted"] += 1
    for up in db.scalars(select(BuyerUpload).where(BuyerUpload.status == BuyerUploadStatus.DRAFT,
                                                   BuyerUpload.created_at < link_cut)):
        up.status, up.session_hash = BuyerUploadStatus.EXPIRED, None
        out["upload_expired"] += 1
    for up in db.scalars(select(BuyerUpload).where(BuyerUpload.created_at < keep_cut,
                                                   BuyerUpload.description.is_not(None) | BuyerUpload.vehicle.is_not(None))):
        up.description = up.vehicle = up.session_hash = None
    if ready():
        try:
            known = set(db.scalars(select(AftersalesFile.r2_upload_id).where(AftersalesFile.r2_state == "UPLOADING")))
            for key, upload_id in r2().stale_multiparts(now - timedelta(days=1)):
                if upload_id not in known:
                    r2().abort(key, upload_id)
                    out["upload_r2_stale"] += 1
        except R2Error as e:
            out["upload_r2_error"] = str(e)[:200]
    db.flush()
    return out


# ------------------------------------------------------------------ 工作台展示
def upload_dict(db: Session, up: BuyerUpload, files: list[dict]) -> dict:
    return {"kind": "upload", "id": up.id, "code": up.code, "order_id": up.order_id, "store_id": up.store_id,
            "conversation_id": up.conversation_id, "buyer_key": up.buyer_key, "item_id": up.item_id,
            "item_title": up.item_title, "issue_type": up.issue_type, "issue_label": ISSUE_LABELS.get(up.issue_type or ""),
            "description": up.description, "vehicle": up.vehicle, "installed": INSTALLED.get(up.installed or ""),
            "language": up.language, "verify_method": up.verify_method, "status": up.status,
            "status_label": BuyerUploadStatus.LABELS.get(up.status, up.status), "created_by": up.created_by,
            "created_at": up.created_at.isoformat(timespec="seconds") + "Z" if up.created_at else None,
            "submitted_at": up.submitted_at.isoformat(timespec="seconds") + "Z" if up.submitted_at else None,
            "files": files}
