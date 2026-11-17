"""创建第一个管理员账号（登录页不提供注册，其余账号由管理员在「系统管理 > 账号管理」里创建）。

用法（项目根目录）：
    python scripts/create_admin.py <登录名>
密码在终端里输入，不回显；要求至少 8 位且不能是纯数字或纯字母。
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.auth import hash_password, password_problem  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import User  # noqa: E402
from app.seed import bootstrap  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    username = sys.argv[1].strip()
    password = getpass.getpass("密码：")
    if password != getpass.getpass("再输一次："):
        print("两次输入不一致")
        return 1
    if problem := password_problem(password):
        print(problem)
        return 1
    bootstrap()  # 建表、写入默认标签与模板（幂等）
    with session_scope() as db:
        if db.scalar(select(User).where(User.username == username)) is not None:
            print(f"账号 {username} 已存在")
            return 1
        db.add(User(username=username, display_name=username, role="ADMIN", password_hash=hash_password(password),
                    all_stores=True))
    print(f"管理员 {username} 已创建，可以登录了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
