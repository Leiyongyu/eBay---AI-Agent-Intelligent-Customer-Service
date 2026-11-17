"""eBay Marketplace Account Deletion 通知端点（第 8.1 节第 4 条、第 18.2 节）。

GET：challenge 验证，返回 sha256(challengeCode + verificationToken + endpoint)。
POST：记录删除请求（只存用户键的哈希）。
尚未实现：X-EBAY-SIGNATURE 验签（需通过 Notification API 取公钥）与异步删除执行，状态记为 RECEIVED_UNVERIFIED。
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import DataDeletionRequest

router = APIRouter(prefix="/webhooks/ebay", tags=["webhooks"])


@router.get("/account-deletion")
def deletion_challenge(challenge_code: str):
    s = get_settings()
    if not (s.ebay_verification_token and s.ebay_deletion_endpoint):
        raise HTTPException(503, "EBAY_VERIFICATION_TOKEN / EBAY_DELETION_ENDPOINT 未配置")
    digest = hashlib.sha256((challenge_code + s.ebay_verification_token + s.ebay_deletion_endpoint).encode()).hexdigest()
    return {"challengeResponse": digest}


@router.post("/account-deletion")
async def deletion_notification(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    notification = payload.get("notification") or {}
    data = notification.get("data") or {}
    user_key = data.get("userId") or data.get("username") or ""
    if not user_key:
        raise HTTPException(400, "通知缺少用户标识")
    nid = notification.get("notificationId")
    if nid and db.scalar(select(DataDeletionRequest).where(DataDeletionRequest.notification_id == nid)):
        return {"ok": True, "duplicate": True}
    db.add(DataDeletionRequest(source="ebay_account_deletion", notification_id=nid,
                               ebay_user_key_hash=hashlib.sha256(user_key.encode()).hexdigest(),
                               status="RECEIVED_UNVERIFIED"))
    return {"ok": True}


# ---------------------------------------------------------------- 实时推送（Notification API，NEW_MESSAGE 等）
@router.get("/notification")
def notification_challenge(challenge_code: str):
    from ..ebay.notifications import challenge_response

    try:
        return {"challengeResponse": challenge_response(challenge_code)}
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


@router.post("/notification", status_code=204)
async def notification(request: Request):
    """验签 → 去重入库 → 后台立刻拉取对应店铺。验签不通过返回 412（与 eBay 官方 SDK 一致），不入库。"""
    import json
    import logging

    from fastapi import Response
    from starlette.concurrency import run_in_threadpool

    from ..ebay import notifications as push

    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "不是 JSON")
    if get_settings().ebay_notify_verify_signature:
        try:
            await run_in_threadpool(push.verify_signature, raw, request.headers.get("x-ebay-signature"), payload)
        except push.SignatureError as e:
            logging.getLogger(__name__).warning("eBay 推送验签失败：%s", e)
            raise HTTPException(412, "签名校验不通过")
    await run_in_threadpool(push.receive, payload)
    return Response(status_code=204)
