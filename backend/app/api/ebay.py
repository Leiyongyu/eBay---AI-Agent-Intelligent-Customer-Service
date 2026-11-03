"""eBay 对接状态与手动同步。不返回任何令牌或密钥，只返回“是否已填写”。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..ebay.auth import token_kind
from ..ebay.scheduler import scheduler
from ..ebay.stores import ebay_not_ready_reasons, ebay_store_targets
from ..ebay.sync import EbayMessageSync
from ..models import Conversation, EbaySyncState, Message
from .deps import iso, reviewer_name

router = APIRouter(prefix="/internal/ebay", tags=["ebay"])

TOKEN_KIND_LABELS = {
    "oauth": "OAuth 令牌",
    "auth_n_auth": "Auth'n'Auth 旧令牌（Message API 不可用）",
    "unknown": "格式无法识别",
    "missing": "未填写",
}


@router.get("/status")
def ebay_status(db: Session = Depends(get_db)):
    s = get_settings()
    reasons = ebay_not_ready_reasons()
    targets = ebay_store_targets()
    all_states = db.scalars(select(EbaySyncState).where(
        EbaySyncState.store_id.in_([t.store_id for t in targets]))).all()
    ebay_convs = select(Conversation.id).where(Conversation.channel_source == "ebay")
    conv_counts = dict(db.execute(select(Conversation.store_id, func.count()).where(Conversation.channel_source == "ebay")
                                  .group_by(Conversation.store_id)).all())

    def state_dict(st):
        return {"conversation_type": st.conversation_type, "last_started_at": iso(st.last_started_at),
                "last_success_at": iso(st.last_success_at), "last_error": st.last_error, "last_stats": st.last_stats}

    states = [st for st in all_states if targets and st.store_id == targets[0].store_id]
    return {
        "ready": not reasons,
        "not_ready_reasons": reasons,
        "env": s.ebay_env,
        "store_id": targets[0].store_id if len(targets) == 1 else None,
        # 多店铺：每个店铺用自己的秘钥同步，进度与错误分开记录（不返回任何秘钥）
        "stores": [{"store_id": t.store_id, "name": t.name, "seller_username": t.seller_username,
                    "marketplace_id": t.marketplace_id, "source": t.source,
                    "conversations": conv_counts.get(t.store_id, 0),
                    "states": [state_dict(st) for st in all_states if st.store_id == t.store_id]} for t in targets],
        "client_id_configured": bool(s.ebay_client_id),
        "client_secret_configured": bool(s.ebay_client_secret),
        "refresh_token": TOKEN_KIND_LABELS[token_kind(s.ebay_refresh_token)],
        "user_access_token_configured": bool(s.ebay_user_access_token),
        "seller_username_configured": bool(s.ebay_seller_username),
        "sync_enabled": s.ebay_sync_enabled,
        "sync_interval_seconds": s.ebay_sync_interval_seconds,
        "scheduler_running": scheduler.running,
        "create_reply_jobs": s.ebay_sync_create_reply_jobs,
        "conversations": db.scalar(select(func.count()).select_from(ebay_convs.subquery())),
        "messages": db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id.in_(ebay_convs))),
        "states": [state_dict(st) for st in states],
    }


@router.post("/sync")
def ebay_sync_now():
    """立即拉取一次（同步执行，通常几秒到几十秒）。"""
    return EbayMessageSync().run()


# ---------------------------------------------------------------- 实时推送订阅
@router.get("/push")
def push_status(db: Session = Depends(get_db)):
    """实时推送：接收地址、订阅情况（每个店铺）、最近收到的推送。"""
    from ..ebay.notifications import status

    return status(db)


class PushSetupBody(BaseModel):
    send_test: bool = False


@router.post("/push/setup")
def push_setup(body: PushSetupBody, reviewer: str = Depends(reviewer_name)):
    """建/复用推送目的地，为所有对接店铺订阅 NEW_MESSAGE（已订阅的跳过）；可让 eBay 给每个订阅发一条测试推送。"""
    from ..ebay.notifications import setup

    return setup(actor=reviewer, send_test=body.send_test)
