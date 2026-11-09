"""会话页“内部参考”：按会话场景列出相关的仅内部条款，给客服、售后看（不发给买家，也不给 AI）。按负责店铺。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import check_store
from ..config import get_settings
from ..db import get_db
from ..knowledge.scenes import internal_refs
from ..models import Conversation

router = APIRouter(prefix="/internal", tags=["internal-refs"])


@router.get("/conversations/{conv_id}/internal-refs")
def conversation_internal_refs(conv_id: int, db: Session = Depends(get_db)):
    conv = db.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    check_store(conv.store_id)
    return internal_refs(db, conv, get_settings().knowledge_date())
