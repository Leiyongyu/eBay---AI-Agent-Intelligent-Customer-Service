"""系统配置页接口：只有管理员能看、能改（路由挂在 require_admin 下）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import runtime_settings
from ..auth import current_user, require_admin
from ..db import get_db

router = APIRouter(prefix="/internal/admin/settings", tags=["settings-admin"], dependencies=[Depends(require_admin)])


class SaveBody(BaseModel):
    values: dict[str, Any]


@router.get("")
def list_settings(db: Session = Depends(get_db)):
    return {"items": runtime_settings.listing(db)}


@router.put("")
def save_settings(body: SaveBody, request: Request, db: Session = Depends(get_db)):
    try:
        changed = runtime_settings.save(db, body.values, current_user(request).display_name)
    except runtime_settings.SettingsError as e:
        raise HTTPException(422, e.message) from None
    return {"changed": changed, "items": runtime_settings.listing(db)}


@router.delete("/{key}")
def reset_setting(key: str, request: Request, db: Session = Depends(get_db)):
    """回到 .env 里的值。"""
    if key not in runtime_settings.KEYS:
        raise HTTPException(404, "没有这个配置项")
    runtime_settings.reset(db, key, current_user(request).display_name)
    return {"items": runtime_settings.listing(db)}
