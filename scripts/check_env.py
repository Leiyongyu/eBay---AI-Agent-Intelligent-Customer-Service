"""连通性自检：PyCharm 里直接 Run 本文件。

依次检查 .env 中的 PostgreSQL、Redis、大模型、向量模型，每项打印 ✅/❌ 和原因；
不打印任何密钥、密码或完整业务空间 ID。全部通过时退出码为 0。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import get_settings  # noqa: E402


def mask_url(url: str | None) -> str:
    if not url:
        return "（未填）"
    return re.sub(r"https://([^.]{0,3})[^.]*\.", r"https://\1***.", url)


def short(e: Exception) -> str:
    msg = str(getattr(e, "orig", e)).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return f"{type(e).__name__}: {msg[:300]}"


def check_database(s) -> tuple[bool, str]:
    if s.using_sqlite_fallback:
        return True, "DATABASE_URL 未配置，使用本地 SQLite（仅开发）"
    if not s.database_password:
        return False, "DATABASE_PASSWORD 未填写"
    from sqlalchemy import text

    from app.db import ensure_database_exists, get_engine

    try:
        created = ensure_database_exists()
        with get_engine().connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
        return True, f"{s.database_display}，PostgreSQL {version}" + ("（已自动建库）" if created else "")
    except Exception as e:
        return False, f"{s.database_display}：{short(e)}"


def check_pgvector(s) -> tuple[bool, str]:
    """只读检查：扩展文件是否装进了 PostgreSQL、当前库是否已启用（启用由应用启动时自动完成）。"""
    if s.using_sqlite_fallback:
        return True, "SQLite 开发模式：向量在 Python 里比对，不需要 pgvector"
    from sqlalchemy import text

    from app.db import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT default_version, installed_version FROM pg_available_extensions "
                                "WHERE name = 'vector'")).first()
    mode = f"（KNOWLEDGE_MODE={s.knowledge_mode}）"
    if row is None:
        return False, "PostgreSQL 里没有 pgvector 扩展文件，检索模式不可用，按开发文档 21 章编译安装" + mode
    if row[1] is None:
        return True, f"已安装 v{row[0]}，当前库尚未启用（启动应用时自动 CREATE EXTENSION）" + mode
    return True, f"已启用 v{row[1]}" + mode


def check_redis(s) -> tuple[bool, str]:
    if not s.redis_url:
        return True, "未配置（Worker 用进程内唤醒）"
    from main import AppLauncher

    from app.redis_bus import bus

    AppLauncher.ensure_redis(s)  # 本机没运行时按 REDIS_AUTOSTART_PATH 拉起
    r = bus.ping()
    return (True, f"{s.redis_url}，版本 {r['server_version']}") if r["ok"] else (False, f"{s.redis_url}：{r.get('error')}")


def check_llm(s) -> tuple[bool, str]:
    reasons = s.agent_not_ready_reasons()
    if reasons:
        return False, "；".join(reasons)
    from langchain_core.messages import HumanMessage

    from app.providers.chat import ChatModelProvider

    started = time.perf_counter()
    try:
        resp = ChatModelProvider(s).model.invoke([HumanMessage(content='只回复 JSON：{"ok": true}')])
    except Exception as e:
        return False, f"{s.llm_chat_model} @ {mask_url(s.llm_base_url)}：{short(e)}"
    usage = getattr(resp, "usage_metadata", None) or {}
    return True, (f"{s.llm_chat_model} @ {mask_url(s.llm_base_url)}，{time.perf_counter() - started:.1f} 秒，"
                  f"回复 {str(resp.content)[:40]!r}，tokens {usage.get('total_tokens', '—')}")


def check_embedding(s) -> tuple[bool, str]:
    from app.providers.embeddings import embedding_ready, probe

    ok, problems = embedding_ready()
    if not ok:
        return False, "；".join(problems)
    started = time.perf_counter()
    try:
        r = probe()
    except Exception as e:
        return False, f"{s.embedding_model} @ {mask_url(s.embedding_base_url)}：{short(e)}"
    return True, f"{r['model']} @ {mask_url(s.embedding_base_url)}，实际维度 {r['dimension']}，{time.perf_counter() - started:.1f} 秒"


def check_ebay(s) -> tuple[bool, str]:
    """换一次访问令牌，再取 1 页买家会话，确认授权范围里有 commerce.message。不写库。"""
    reasons = s.ebay_not_ready_reasons()
    if reasons:
        return False, "；".join(reasons)
    from app.ebay.auth import EbayAuthError, token_kind
    from app.ebay.message_api import EbayApiError, MessageApiClient

    if s.ebay_refresh_token and token_kind(s.ebay_refresh_token) == "auth_n_auth":
        return False, "EBAY_REFRESH_TOKEN 是 Auth'n'Auth 旧令牌（AgAAAA 开头），Message API 不接受，需要 OAuth refresh token"
    started = time.perf_counter()
    try:
        client = MessageApiClient()
        data = client._get("/conversation", {"conversation_type": "FROM_MEMBERS", "limit": 1, "offset": 0})
    except (EbayAuthError, EbayApiError) as e:
        return False, f"{s.ebay_env}：{e}"
    return True, (f"{s.ebay_env} 环境，令牌有效，消息接口可读；近期买家会话共 {data.get('total', 0)} 个，"
                  f"{time.perf_counter() - started:.1f} 秒")


def main() -> int:
    s = get_settings()
    results = []
    checks = [("PostgreSQL", check_database), ("pgvector", check_pgvector), ("Redis", check_redis), ("大模型", check_llm),
              ("向量模型", check_embedding)]
    if s.ebay_client_id or s.ebay_refresh_token or s.ebay_user_access_token:
        checks.append(("eBay 消息接口", check_ebay))
    for name, fn in checks:
        try:
            ok, detail = fn(s)
        except Exception as e:
            ok, detail = False, short(e)
        results.append(ok)
        print(f"{'✅' if ok else '❌'} {name}：{detail}", flush=True)
    print("全部通过" if all(results) else "有未通过的项目，按上面的原因修改 .env 后重新运行")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
