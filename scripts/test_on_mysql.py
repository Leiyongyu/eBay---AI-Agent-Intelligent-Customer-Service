"""在本机 MySQL 上跑测试（业务库 MySQL + 表名前缀 cs_，向量库临时 SQLite）。

每个测试新建 ebaycs_test_xxxx 库、跑完删掉，不碰 jmh_ai_agent 和其他库。账号默认 root@127.0.0.1:3306，
密码取 .env 的 DATABASE_PASSWORD（和 PostgreSQL 同一个），不打印。其余参数原样交给 pytest，例如：
    python scripts/test_on_mysql.py -q
    python scripts/test_on_mysql.py tests/test_aftersales_reasons.py -x
可用 MYSQL_TEST_HOST / MYSQL_TEST_PORT / MYSQL_TEST_USER 改连接。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    from app.config import get_settings

    pwd = os.environ.get("TEST_MYSQL_PASSWORD") or get_settings().database_password or ""
    host = os.environ.get("MYSQL_TEST_HOST", "127.0.0.1")
    port = os.environ.get("MYSQL_TEST_PORT", "3306")
    user = os.environ.get("MYSQL_TEST_USER", "root")
    env = {**os.environ, "TEST_MYSQL_URL": f"mysql+pymysql://{user}@{host}:{port}/?charset=utf8mb4",
           "TEST_MYSQL_PASSWORD": pwd}
    return subprocess.call([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *sys.argv[1:]], cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
