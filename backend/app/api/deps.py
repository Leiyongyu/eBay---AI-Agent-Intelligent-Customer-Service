"""接口公共依赖。

操作人：一律取当前登录账号的显示名（登录与权限见 app/auth.py），不再信任前端传来的审核员名。
"""
from __future__ import annotations

from fastapi import Request

from ..auth import current_user


def reviewer_name(request: Request) -> str:
    return current_user(request).display_name[:64]


def optional_reviewer(request: Request) -> str:
    return current_user(request).display_name[:64]


def iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None
