"""可观测性：trace_id 贯穿一次请求 / 一次起草 / 一次发送，日志可选 JSON 结构化输出（第二批 B7）。

trace_id 怎么来：
  - HTTP 请求：带了 X-Trace-Id（或 X-Request-ID，字母数字 - _，≤ 64 位）就沿用，没带就新生成；响应头回 X-Trace-Id。
  - Worker 起草一个回复任务：每次运行新生成一个，记在 agent_runs.trace_id。
  - 发件箱发信：沿用创建这条待发消息时的 trace_id（审核员批准草稿的那次请求），审批到发出是同一个 trace。
  - 后台同步线程：每轮可用 trace_scope 包一层；没包的日志 trace_id 为空，带线程名。
  新建的 agent_runs、audit_events、llm_usage、tool_calls、outbound_messages 在 flush 前自动填当前 trace_id
  （install_db_hook），不用每处手写。

日志：LOG_FORMAT=text（默认，和以前一样，行首多一个 [trace_id]）/ json（一行一个 JSON：ts、level、logger、msg、
  trace_id、store_id、conversation_id、job_id、thread、exc）；LOG_FILE 填了同时写到滚动文件（LOG_FILE_MAX_MB × LOG_FILE_BACKUPS）。
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_fields: contextvars.ContextVar[dict] = contextvars.ContextVar("trace_fields", default={})

HEADER = "X-Trace-Id"
INCOMING_HEADERS = ("x-trace-id", "x-request-id")
_VALID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
BOUND_KEYS = ("store_id", "conversation_id", "job_id")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:
    return _trace.get()


def current_fields() -> dict:
    return dict(_fields.get())


def valid_trace_id(value: str | None) -> str | None:
    return value if value and _VALID.match(value) else None


@contextmanager
def trace_scope(trace_id: str | None = None, *, new: bool = False, **fields: Any) -> Iterator[str]:
    """进入一个 trace：trace_id 给了用它；new=True 强制新开；否则沿用当前的（没有就新开）。fields 绑到日志上。"""
    tid = valid_trace_id(trace_id) or (None if new else _trace.get()) or new_trace_id()
    t1 = _trace.set(tid)
    t2 = _fields.set({**_fields.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield tid
    finally:
        _fields.reset(t2)
        _trace.reset(t1)


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """只追加日志字段（店铺、会话、任务），不换 trace。"""
    tok = _fields.set({**_fields.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _fields.reset(tok)


# ------------------------------------------------------------------ 日志
class TraceFilter(logging.Filter):
    """把 trace_id 和绑定字段挂到每条日志上（text / json 两种格式都用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace.get() or "-"
        f = _fields.get()
        for k in BOUND_KEYS:
            if not hasattr(record, k):
                setattr(record, k, f.get(k))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) if getattr(record, "trace_id", "-") != "-" else None,
            "thread": record.threadName,
        }
        for k in BOUND_KEYS:
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        extra = getattr(record, "event", None)
        if isinstance(extra, dict):  # log.info("...", extra={"event": {...}}) 附加结构化字段
            out.update({k: v for k, v in extra.items() if k not in out})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(out, ensure_ascii=False, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s [%(trace_id)s] [%(name)s] %(message)s"


def build_formatter(fmt: str) -> logging.Formatter:
    return JsonFormatter() if (fmt or "").lower() == "json" else logging.Formatter(TEXT_FORMAT)


def setup_logging(settings) -> None:
    """main.py 读到配置后调用：重设根日志的处理器（控制台 + 可选滚动文件），统一挂 TraceFilter。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    formatter = build_formatter(settings.log_format)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_file:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=max(1, settings.log_file_max_mb) * 1024 * 1024, backupCount=max(0, settings.log_file_backups),
            encoding="utf-8"))
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(TraceFilter())
        root.addHandler(h)
    root.setLevel(settings.log_level.upper())


# ------------------------------------------------------------------ HTTP 中间件
access_log = logging.getLogger("app.access")


async def trace_middleware(request, call_next):
    """每个请求一个 trace：沿用请求头里的，或新生成；响应头回 X-Trace-Id；记一行访问日志（不含查询参数和请求体）。"""
    incoming = next((request.headers.get(h) for h in INCOMING_HEADERS if request.headers.get(h)), None)
    with trace_scope(incoming, new=True) as tid:
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[HEADER] = tid
            return response
        finally:
            ms = int((time.perf_counter() - started) * 1000)
            path = request.url.path
            if path.startswith(("/api", "/internal", "/webhooks", "/upload")):  # 静态资源不记
                access_log.log(logging.WARNING if status >= 500 else logging.INFO,
                               "%s %s %s %dms", request.method, path, status, ms,
                               extra={"event": {"method": request.method, "path": path, "status": status, "duration_ms": ms}})


# ------------------------------------------------------------------ 入库时自动带 trace_id
_installed = False


def install_db_hook() -> None:
    """新建的带 trace_id 字段的行（运行记录、审计、模型用量、工具调用、待发消息）在 flush 前自动填当前 trace_id。"""
    global _installed
    if _installed:
        return
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    @event.listens_for(Session, "before_flush")
    def _fill_trace(session, flush_context, instances):  # noqa: ARG001
        tid = _trace.get()
        if not tid:
            return
        for obj in session.new:
            if hasattr(obj, "trace_id") and getattr(obj, "trace_id", None) is None:
                try:
                    obj.trace_id = tid
                except AttributeError:
                    pass

    _installed = True
