"""ToolExecutor：白名单、参数校验、预算、计时与留痕（第 12.2 节）。

P2 固定流程的预取和 P3 Planner 的调用都必须经过这里。
"""
from __future__ import annotations

import time

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..agent.schemas import RuntimeContext, ToolResult
from ..db import utcnow
from ..models import ToolCall
from ..sources import BusinessSource, SourceNotAvailable
from .registry import TOOLS


class ToolBudgetExceeded(RuntimeError):
    pass


class ToolExecutor:
    def __init__(self, db: Session, ctx: RuntimeContext, source: BusinessSource, max_calls: int):
        self.db = db
        self.ctx = ctx
        self.source = source
        self.max_calls = max_calls
        self.calls = 0

    def execute(self, tool_name: str, arguments: dict | None = None, request_id: str | None = None) -> ToolResult:
        self.calls += 1
        request_id = request_id or f"step-{self.calls}"
        now = utcnow()
        if self.calls > self.max_calls:
            raise ToolBudgetExceeded(f"工具调用超过上限 {self.max_calls}")

        spec = TOOLS.get(tool_name)
        if spec is None:
            return self._record(tool_name, "0", request_id, arguments, "ERROR", None, None, "工具不在白名单", 0, now)
        try:
            args = spec.args_model.model_validate(arguments or {})
        except ValidationError as e:
            return self._record(tool_name, spec.version, request_id, arguments, "ERROR", None, None,
                                f"参数无效：{e.errors()[0]['msg']}", 0, now)

        started = time.perf_counter()
        try:
            extra = {"db": self.db} if spec.needs_db else {}
            status, data, evidence_ref, message = spec.fn(self.ctx, self.source, args, **extra)
        except SourceNotAvailable as e:
            status, data, evidence_ref, message = "ERROR", None, None, str(e)
        except Exception as e:  # 数据源异常不能伪装成“没有数据”
            status, data, evidence_ref, message = "ERROR", None, None, f"数据源异常：{type(e).__name__}"
        duration = int((time.perf_counter() - started) * 1000)
        return self._record(tool_name, spec.version, request_id, args.model_dump(exclude_none=True),
                            status, data, evidence_ref, message, duration, now)

    def _record(self, tool_name, version, request_id, arguments, status, data, evidence_ref, message, duration, now):
        result = ToolResult(
            tool_name=tool_name,
            request_id=request_id,
            status=status,
            data=data,
            source=self.source.name,
            queried_at=now.isoformat(timespec="seconds") + "Z",
            evidence_ref=evidence_ref,
            message=message,
        )
        self.db.add(ToolCall(
            job_id=self.ctx.job_id,
            run_generation=self.ctx.run_generation,
            request_id=request_id,
            tool_name=tool_name,
            tool_version=version,
            arguments_redacted=arguments or {},
            result_status=status,
            result_data={"data": data, "message": message},
            source=self.source.name,
            evidence_ref=evidence_ref,
            duration_ms=duration,
            queried_at=now,
        ))
        self.db.flush()
        return result
