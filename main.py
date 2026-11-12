"""主启动类：在 PyCharm 里右键 Run 'main'（Shift+F10）即可。

不带任何参数就跑完整流程：
  1. 检查 Python 版本与依赖（在虚拟环境里缺依赖时自动 pip install -r requirements.txt）
  2. 首次运行把 .env.example 复制为 .env（模型、密钥都在 .env 里手动填写）
  3. 前端源码有改动时自动 npm install / npm run build
  4. 端口被占用时自动顺延到下一个空闲端口
  5. 启动 FastAPI（含后台 Worker），就绪后自动打开浏览器
可选反向开关：--no-browser（不打开浏览器）、--no-build（跳过前端构建）
"""
from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
REQUIRED_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "pydantic_settings", "langgraph", "langchain_core",
                    "langchain_openai", "tzdata", "psycopg", "redis")

log = logging.getLogger("launcher")


class AppLauncher:
    def __init__(self, argv: list[str] | None = None):
        argv = sys.argv[1:] if argv is None else argv
        self.open_browser_flag = "--no-browser" not in argv
        self.build_flag = "--no-build" not in argv

    # ------------------------------------------------------------------ 入口
    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
        self.check_python()
        self.ensure_dependencies()
        self.ensure_env_file()
        sys.path.insert(0, str(BACKEND))

        from app.config import get_settings

        settings = get_settings()
        logging.getLogger().setLevel(settings.log_level.upper())
        self.check_database(settings)
        self.ensure_redis(settings)
        if self.build_flag and settings.frontend_auto_build:
            self.build_frontend()
        # 开了 Cloudflare 隧道时端口必须固定：隧道在 Cloudflare 后台写死转到 localhost:APP_PORT
        port = self.pick_port(settings.app_host, settings.app_port,
                              strict=bool(settings.cloudflared_path and settings.cloudflare_tunnel_token))
        lan = settings.app_host in ("0.0.0.0", "::")  # 监听全部网卡：局域网里的电脑也能访问
        url = f"http://{'127.0.0.1' if lan else settings.app_host}:{port}/"
        if lan:
            log.info("局域网访问地址：%s（其他电脑用这个地址；需在 Windows 防火墙放行 TCP %s 端口）",
                     "、".join(f"http://{ip}:{port}/" for ip in self.lan_ips()) or "（没找到局域网 IP）", port)
        if self.open_browser_flag and settings.app_open_browser:
            threading.Thread(target=self.open_browser_when_ready, args=(url,), daemon=True).start()
        self.serve(settings.app_host, port, url)

    # ------------------------------------------------------------------ 环境
    @staticmethod
    def check_python() -> None:
        if sys.version_info < (3, 11):
            sys.exit(f"需要 Python 3.11+，当前是 {sys.version.split()[0]}。请在 PyCharm 中把解释器设为项目下的 .venv。")

    @staticmethod
    def _missing_modules() -> list[str]:
        import importlib.util

        return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]

    def ensure_dependencies(self) -> None:
        missing = self._missing_modules()
        if not missing:
            return
        in_venv = sys.prefix != sys.base_prefix
        if not in_venv:
            sys.exit(
                f"缺少依赖：{', '.join(missing)}。\n"
                "当前解释器不是虚拟环境，为避免装进全局 Python，不自动安装。请在 PyCharm：\n"
                "  Settings → Project → Python Interpreter → Add Interpreter → Existing → 选择 .venv\\Scripts\\python.exe\n"
                "或在终端执行：python -m pip install -r requirements.txt"
            )
        log.info("虚拟环境缺少依赖 %s，自动安装 requirements.txt ...", missing)
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], check=True)

    @staticmethod
    def ensure_env_file() -> None:
        env, example = ROOT / ".env", ROOT / ".env.example"
        if not env.exists() and example.exists():
            shutil.copyfile(example, env)
            log.warning("已根据 .env.example 生成 .env：请在其中填写 LLM_* 等模型配置（未填写时为 AGENT_NOT_READY）")

    # ------------------------------------------------------------------ 数据库与 Redis
    @staticmethod
    def check_database(settings) -> None:
        """业务库（MySQL / PostgreSQL）库不存在就自动创建；向量库（PostgreSQL）单独配置时也连一下。
        连不上时给出明确原因后退出，不静默回退到 SQLite。"""
        if settings.using_sqlite_fallback:
            log.warning("DATABASE_URL 未配置，使用本地 SQLite（仅开发用）")
            return
        from sqlalchemy import text

        from sqlalchemy.engine import make_url

        from app.db import ensure_database_exists, get_engine, get_vector_engine, vector_separate

        names = {"mysql": "MySQL", "postgresql": "PostgreSQL"}
        try:
            if ensure_database_exists():
                log.info("已自动创建数据库：%s", settings.database_display)
            engine = get_engine()
            with engine.connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar()  # MySQL、PostgreSQL 都支持
            log.info("业务库已连接：%s（%s %s，表名前缀 %s）", settings.database_display,
                     names.get(engine.dialect.name, engine.dialect.name), str(version)[:40], settings.table_prefix or "无")
        except Exception as e:
            msg = str(getattr(e, "orig", e)).strip().splitlines()[0]
            hint = "请在 .env 填写 DATABASE_PASSWORD" if not settings.database_password else \
                "请核对 .env 中的 DATABASE_URL / DATABASE_PASSWORD，以及数据库服务（MySQL / PostgreSQL）是否在运行"
            sys.exit(f"连接数据库失败：{settings.database_display}\n原因：{msg}\n{hint}")
        if not vector_separate():
            return
        shown = make_url(settings.effective_vector_database_url).render_as_string(hide_password=True)
        try:
            with get_vector_engine().connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar()
            log.info("向量库已连接：%s（%s）", shown, str(version)[:40])
        except Exception as e:  # 向量库连不上只影响知识检索（检索模式下交人工），不阻止启动
            log.warning("向量库连接失败：%s：%s（知识检索会报未就绪）", shown, str(getattr(e, "orig", e)).strip().splitlines()[0])

    @staticmethod
    def ensure_redis(settings) -> None:
        """REDIS_URL 指向本机且连不上时，用 REDIS_AUTOSTART_PATH 拉起 redis-server（独立进程，程序退出后继续运行）。"""
        if not settings.redis_url:
            return
        from urllib.parse import urlparse

        from app.redis_bus import bus

        if bus.ping()["ok"]:
            log.info("Redis 已连接：%s", settings.redis_url)
            return
        parsed = urlparse(settings.redis_url)
        exe = Path(settings.redis_autostart_path) if settings.redis_autostart_path else None
        if parsed.hostname not in ("127.0.0.1", "localhost") or exe is None or not exe.exists():
            log.warning("Redis 连不上（%s），Worker 退回进程内唤醒，功能不受影响", settings.redis_url)
            return
        conf = exe.parent / "redis.windows.conf"
        # 端口以 REDIS_URL 为准，覆盖配置文件里的 port
        args = [str(exe)] + ([str(conf)] if conf.exists() else []) + ["--port", str(parsed.port or 6379)]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, cwd=exe.parent, creationflags=flags, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        for _ in range(20):
            time.sleep(0.25)
            if bus.ping()["ok"]:
                log.info("已启动本机 Redis：%s（独立进程，关闭本程序后仍在运行）", exe)
                return
        log.warning("尝试启动 Redis 失败，Worker 退回进程内唤醒")

    # ------------------------------------------------------------------ 前端
    @staticmethod
    def _latest_mtime(paths) -> float:
        latest = 0.0
        for p in paths:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        latest = max(latest, f.stat().st_mtime)
            elif p.exists():
                latest = max(latest, p.stat().st_mtime)
        return latest

    def build_frontend(self) -> None:
        dist_index = FRONTEND / "dist" / "index.html"
        sources = [FRONTEND / "src", FRONTEND / "index.html", FRONTEND / "package.json", FRONTEND / "vite.config.ts"]
        if dist_index.exists() and dist_index.stat().st_mtime >= self._latest_mtime(sources):
            return
        npm = shutil.which("npm")
        if npm is None:
            log.warning("未找到 npm（需要 Node.js 20.19+），跳过前端构建%s",
                        "，使用已有 dist" if dist_index.exists() else "；只能访问接口文档 /docs")
            return
        lock = FRONTEND / "node_modules" / ".package-lock.json"
        if not lock.exists() or lock.stat().st_mtime < (FRONTEND / "package.json").stat().st_mtime:
            log.info("安装前端依赖 npm install ...")
            subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)
        log.info("构建前端 npm run build ...")
        subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)

    # ------------------------------------------------------------------ 服务
    @staticmethod
    def lan_ips() -> list[str]:
        """本机的局域网 IPv4 地址（192.168.* / 10.* / 172.16–31.*）。"""
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        def private(ip: str) -> bool:
            a, b_ = (int(x) for x in ip.split(".")[:2])
            return a == 10 or (a == 192 and b_ == 168) or (a == 172 and 16 <= b_ <= 31)
        return sorted(ip for ip in ips if private(ip))

    @staticmethod
    def pick_port(host: str, preferred: int, strict: bool = False) -> int:
        for port in range(preferred, preferred + (1 if strict else 20)):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((host, port))
                    if port != preferred:
                        log.warning("端口 %s 已被占用（可能是上一次运行未退出），改用 %s", preferred, port)
                    return port
                except OSError:
                    continue
        if strict:
            sys.exit(f"端口 {preferred} 被占用（可能是上一次运行未退出）。eBay 推送经 Cloudflare 隧道固定转到这个端口，"
                     f"请先结束占用它的程序再启动")
        sys.exit(f"{preferred}–{preferred + 19} 端口都被占用，请在 .env 修改 APP_PORT")

    @staticmethod
    def open_browser_when_ready(url: str, timeout: float = 60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url + "api/health", timeout=2) as resp:
                    if resp.status == 200:
                        webbrowser.open(url)
                        return
            except OSError:
                time.sleep(0.5)
        log.warning("服务 %s 秒内未就绪，未自动打开浏览器", timeout)

    @staticmethod
    def serve(host: str, port: int, url: str) -> None:
        import uvicorn

        from app.server import create_app

        log.info("工作台地址：%s   接口文档：%sdocs", url, url)
        uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    AppLauncher().run()
