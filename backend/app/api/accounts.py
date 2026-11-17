"""登录、退出、改密码，以及管理员的账号管理（只有管理员能建账号、分配店铺、重置密码、停用、删除；登录名由系统随机生成）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import auth
from ..auth import CurrentUser, current_user, require_admin
from ..config import get_settings
from ..db import get_db, utcnow
from ..models import AuditEvent, Role, Store, User, UserSession, UserStore
from .deps import iso

router = APIRouter(prefix="/api", tags=["accounts"])


def _me(user: CurrentUser) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role,
            "role_label": Role.LABELS.get(user.role, user.role), "all_stores": user.sees_all_stores,
            "store_ids": sorted(user.store_ids), "must_change_password": user.must_change_password}


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    token, user = auth.login(body.username, body.password, request.client.host if request.client else None,
                             request.headers.get("user-agent"))
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", path="/",
                        max_age=get_settings().auth_session_hours * 3600 * 14)  # 有效期以服务端会话为准
    return _me(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: CurrentUser = Depends(current_user)):
    # 功能开关（前端据此显示菜单，后端接口另行校验）
    return {**_me(user), "features": {"aftersales_files": user.is_admin or user.can_aftersales_files}}


class PasswordBody(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


@router.post("/auth/password")
def change_password(body: PasswordBody, request: Request, user: CurrentUser = Depends(current_user),
                    db: Session = Depends(get_db)):
    row = db.get(User, user.id)
    if not auth.verify_password(body.old_password, row.password_hash):
        raise HTTPException(400, "原密码不对")
    problem = auth.password_problem(body.new_password)
    if problem:
        raise HTTPException(422, problem)
    row.password_hash, row.must_change_password, row.updated_at = auth.hash_password(body.new_password), False, utcnow()
    auth.revoke_user_sessions(db, row.id, keep_token=request.cookies.get(auth.COOKIE))  # 其他地方登录的一律退出
    db.add(AuditEvent(actor=user.display_name, action="PASSWORD_CHANGED", detail={"user": row.username}))
    return {"ok": True}


# ---------------------------------------------------------------- 管理员：账号管理
def _user_dict(db: Session, u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "role_label": Role.LABELS.get(u.role, u.role), "all_stores": u.role == Role.ADMIN or bool(u.all_stores),
            "store_ids": sorted(db.scalars(select(UserStore.store_id).where(UserStore.user_id == u.id))),
            "is_active": u.is_active, "must_change_password": u.must_change_password,
            "can_aftersales_files": u.role == Role.ADMIN or bool(u.can_aftersales_files),
            "locked": bool(u.locked_until and u.locked_until > utcnow()),
            "last_login_at": iso(u.last_login_at), "created_by": u.created_by, "created_at": iso(u.created_at)}


def _set_stores(db: Session, user: User, store_ids: list[str]) -> None:
    known = set(db.scalars(select(Store.id)))
    bad = [s for s in store_ids if s not in known]
    if bad:
        raise HTTPException(422, f"店铺不存在：{'、'.join(bad[:5])}")
    db.execute(delete(UserStore).where(UserStore.user_id == user.id))
    for sid in dict.fromkeys(store_ids):
        db.add(UserStore(user_id=user.id, store_id=sid))


# 登录名：系统随机生成，字母开头、字母 + 数字；去掉容易看错的 0 o 1 l i
_LETTERS = "abcdefghjkmnpqrstuvwxyz"
_DIGITS = "23456789"


def random_username(db: Session, length: int = 8) -> str:
    import secrets

    for _ in range(50):
        chars = [secrets.choice(_LETTERS)] + [secrets.choice(_LETTERS + _DIGITS) for _ in range(length - 1)]
        if not any(c in _DIGITS for c in chars):
            chars[secrets.randbelow(length - 1) + 1] = secrets.choice(_DIGITS)
        name = "".join(chars)
        if not db.scalar(select(User.id).where(User.username == name)):
            return name
    raise HTTPException(500, "生成登录名失败，请重试")


@router.get("/admin/users/new-username")
def new_username(admin: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    """新建账号时预先生成一个随机登录名（字母 + 数字），可以换一个。"""
    return {"username": random_username(db)}


@router.get("/admin/users")
def list_users(admin: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    return [_user_dict(db, u) for u in db.scalars(select(User).order_by(User.id))]


class CreateUserBody(BaseModel):
    # 登录名由系统随机生成（GET /admin/users/new-username 预先给出）；不传就当场生成
    username: str | None = Field(default=None, min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    display_name: str = Field(min_length=1, max_length=64)
    role: str = Field(pattern="^(ADMIN|CS|AFTERSALES)$")
    password: str = Field(max_length=128)
    all_stores: bool = False
    store_ids: list[str] = Field(default_factory=list, max_length=500)
    can_aftersales_files: bool = False  # 可看售后附件


@router.post("/admin/users")
def create_user(body: CreateUserBody, admin: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    username = (body.username or "").strip().lower() or random_username(db)
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "登录名已存在")
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(422, problem)
    user = User(username=username, display_name=body.display_name.strip(), role=body.role,
                password_hash=auth.hash_password(body.password), all_stores=body.all_stores,
                can_aftersales_files=body.can_aftersales_files, must_change_password=True, created_by=admin.username)
    db.add(user)
    db.flush()
    _set_stores(db, user, body.store_ids)
    db.add(AuditEvent(actor=admin.display_name, action="USER_CREATED",
                      detail={"user": username, "role": body.role, "stores": len(body.store_ids),
                              "all_stores": body.all_stores}))
    db.flush()
    return _user_dict(db, user)


class UpdateUserBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role: str | None = Field(default=None, pattern="^(ADMIN|CS|AFTERSALES)$")
    all_stores: bool | None = None
    store_ids: list[str] | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    can_aftersales_files: bool | None = None
    new_password: str | None = Field(default=None, max_length=128)  # 管理员重置密码：对方下次登录须修改
    unlock: bool = False


@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, body: UpdateUserBody, admin: CurrentUser = Depends(require_admin),
                db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "账号不存在")
    if user.id == admin.id and (body.is_active is False or (body.role and body.role != Role.ADMIN)):
        raise HTTPException(409, "不能停用自己或取消自己的管理员身份")
    changes = []
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
        changes.append("显示名")
    if body.role is not None and body.role != user.role:
        user.role = body.role
        changes.append("角色")
    if body.all_stores is not None:
        user.all_stores = body.all_stores
        changes.append("全部店铺")
    if body.store_ids is not None:
        _set_stores(db, user, body.store_ids)
        changes.append("负责店铺")
    if body.can_aftersales_files is not None and body.can_aftersales_files != bool(user.can_aftersales_files):
        user.can_aftersales_files = body.can_aftersales_files
        changes.append("可看售后附件" if body.can_aftersales_files else "取消售后附件")
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        changes.append("启用" if body.is_active else "停用")
        if not body.is_active:
            auth.revoke_user_sessions(db, user.id)
    if body.new_password:
        problem = auth.password_problem(body.new_password)
        if problem:
            raise HTTPException(422, problem)
        user.password_hash, user.must_change_password = auth.hash_password(body.new_password), True
        auth.revoke_user_sessions(db, user.id)
        changes.append("重置密码")
    if body.unlock:
        user.locked_until, user.failed_logins = None, 0
        changes.append("解锁")
    user.updated_at = utcnow()
    db.add(AuditEvent(actor=admin.display_name, action="USER_UPDATED", detail={"user": user.username, "changes": changes}))
    db.flush()
    return _user_dict(db, user)


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: int, admin: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    """删除账号：登录会话、负责店铺一起删；不能删自己，也不能删掉最后一个启用的管理员。
    历史记录（审计、回复、标记等）里记的是名字，删除后照样保留。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "账号不存在")
    if user.id == admin.id:
        raise HTTPException(409, "不能删除自己的账号")
    if user.role == Role.ADMIN and user.is_active:
        admins = db.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN, User.is_active.is_(True))) or 0
        if admins <= 1:
            raise HTTPException(409, "这是最后一个启用的管理员，不能删除")
    auth.revoke_user_sessions(db, user.id)
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    db.execute(delete(UserStore).where(UserStore.user_id == user.id))
    db.add(AuditEvent(actor=admin.display_name, action="USER_DELETED",
                      detail={"user": user.username, "display_name": user.display_name, "role": user.role}))
    db.delete(user)
    db.flush()
    return {"deleted": user_id}
