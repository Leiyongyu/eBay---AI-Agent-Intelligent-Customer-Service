"""登录与权限（第 18.3 节）。

- 账号只有管理员能建；密码只存 scrypt 哈希；连续失败 5 次锁定 10 分钟。
- 登录后浏览器 Cookie（HttpOnly、SameSite=Lax）里只放随机令牌，库里只存它的 SHA-256；闲置超过 AUTH_SESSION_HOURS 过期。
- 中间件对 /api、/internal 下除公开地址外的所有请求校验登录，未登录返回 401；当前账号放进 request.state 与上下文变量。
- 角色：ADMIN 管理员（全部店铺、账号与系统设置）、CS 客服（分配店铺的来信，回复不了的转售后）、AFTERSALES 售后（处理转售后的会话）。
- 店铺范围：客服、售后只看得到、只处理分配给自己的店铺（或“全部店铺”）；越权访问一律当作不存在（404）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .config import get_settings
from .db import session_scope, utcnow
from .models import Role, User, UserSession, UserStore

COOKIE = "cs_session"
MAX_FAILED = 5
LOCK_MINUTES = 10
# 不需要登录的地址：健康检查、登录本身；eBay 推送 /webhooks 不在 /api、/internal 下，本来就不拦
PUBLIC_PATHS = {"/api/health", "/api/auth/login"}


# ---------------------------------------------------------------- 密码
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "密码至少 8 位"
    if password.isdigit() or password.isalpha():
        return "密码要同时包含字母和数字（或符号）"
    return None


# ---------------------------------------------------------------- 当前账号
@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    display_name: str
    role: str
    all_stores: bool
    store_ids: frozenset[str] = field(default_factory=frozenset)
    must_change_password: bool = False
    can_aftersales_files: bool = False  # 可看售后附件（管理员始终可看）

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def sees_all_stores(self) -> bool:
        return self.is_admin or self.all_stores

    def can_see_store(self, store_id: str | None) -> bool:
        return self.sees_all_stores or (store_id is not None and store_id in self.store_ids)


_current: ContextVar[CurrentUser | None] = ContextVar("current_user", default=None)


def current() -> CurrentUser | None:
    """当前请求的登录账号（中间件设置；后台线程、脚本里为 None，不做店铺限制）。"""
    return _current.get()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_user(db, user: User) -> CurrentUser:
    stores = frozenset(db.scalars(select(UserStore.store_id).where(UserStore.user_id == user.id)))
    return CurrentUser(user.id, user.username, user.display_name, user.role, bool(user.all_stores), stores,
                       bool(user.must_change_password), bool(user.can_aftersales_files))


def resolve_session(token: str | None) -> CurrentUser | None:
    if not token:
        return None
    now = utcnow()
    with session_scope() as db:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
        if sess is None or sess.revoked_at or sess.expires_at < now:
            return None
        user = db.get(User, sess.user_id)
        if user is None or not user.is_active:
            return None
        if (now - sess.last_seen_at).total_seconds() > 300:  # 顺延过期时间，最多 5 分钟写一次库
            sess.last_seen_at = now
            sess.expires_at = now + timedelta(hours=get_settings().auth_session_hours)
        return _load_user(db, user)


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not (path.startswith("/api/") or path.startswith("/internal/")) or path in PUBLIC_PATHS:
        return await call_next(request)
    from starlette.concurrency import run_in_threadpool

    user = await run_in_threadpool(resolve_session, request.cookies.get(COOKIE))
    if user is None:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    if user.must_change_password and path not in ("/api/auth/me", "/api/auth/password", "/api/auth/logout",
                                                  "/api/system/meta"):
        return JSONResponse({"detail": "请先修改初始密码"}, status_code=403)
    request.state.user = user
    token = _current.set(user)
    try:
        return await call_next(request)
    finally:
        _current.reset(token)


# ---------------------------------------------------------------- 依赖
def current_user(request: Request) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


def require_roles(*roles: str):
    def dep(request: Request) -> CurrentUser:
        user = current_user(request)
        if user.role not in roles:
            names = "、".join(Role.LABELS[r] for r in roles)
            raise HTTPException(403, f"只有{names}可以做这个操作")
        return user
    return dep


require_admin = require_roles(Role.ADMIN)


def allowed_store_ids() -> set[str] | None:
    """当前账号能看的店铺；None 表示全部（管理员、全部店铺账号、后台任务）。"""
    user = current()
    if user is None or user.sees_all_stores:
        return None
    return set(user.store_ids)


def check_store(store_id: str | None) -> None:
    user = current()
    if user is not None and not user.can_see_store(store_id):
        raise HTTPException(404, "不存在或不在你负责的店铺")


# ---------------------------------------------------------------- 登录 / 退出
def login(username: str, password: str, ip: str | None, user_agent: str | None) -> tuple[str, CurrentUser]:
    now = utcnow()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == username.strip().lower()))
        if user is None or not user.is_active:
            raise HTTPException(401, "账号或密码不对")
        if user.locked_until and user.locked_until > now:
            raise HTTPException(429, f"连续输错次数过多，请 {LOCK_MINUTES} 分钟后再试")
        if not verify_password(password, user.password_hash):
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= MAX_FAILED:
                user.locked_until, user.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
            db.commit()
            raise HTTPException(401, "账号或密码不对")
        user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
        token = secrets.token_urlsafe(32)
        db.add(UserSession(token_hash=_token_hash(token), user_id=user.id, created_at=now, last_seen_at=now,
                           expires_at=now + timedelta(hours=get_settings().auth_session_hours),
                           ip=(ip or "")[:64] or None, user_agent=(user_agent or "")[:300] or None))
        return token, _load_user(db, user)


def logout(token: str | None) -> None:
    if not token:
        return
    with session_scope() as db:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
        if sess and not sess.revoked_at:
            sess.revoked_at = utcnow()


def revoke_user_sessions(db, user_id: int, keep_token: str | None = None) -> None:
    keep = _token_hash(keep_token) if keep_token else None
    for sess in db.scalars(select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))):
        if sess.token_hash != keep:
            sess.revoked_at = utcnow()
