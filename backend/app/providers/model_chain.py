"""模型链与熔断：同一服务商（百炼）的多个模型按顺序用，第一个主用；某个模型失败 / 额度用完就冷却，自动切下一个。

配置（.env / 系统配置）：
  LLM_CHAT_MODELS        回复起草的模型链（逗号分隔）；空 = 只用 LLM_CHAT_MODEL（兼容旧配置）
  LLM_TRANSLATE_MODELS   翻译的模型链；空 = LLM_TRANSLATE_MODEL，再空 = 聊天链
  VISION_MODELS          识图的模型链；空 = 只用 VISION_MODEL
  LLM_MODEL_OPTIONS      每个模型自己的 extra_body / api_key / base_url（JSON，按模型名）
  LLM_LIGHT_MODELS       “轻量档”模型链；空 = 轻量档也走主力链
向量模型不进链（换模型要重建索引）。

熔断（breaker，进程内共享）：
  - 额度类错误（403 FreeTierOnly、额度 / 配额用完、欠费）：立即冷却 LLM_BREAKER_QUOTA_HOURS（默认 6 小时）；
  - 超时：立即冷却 LLM_BREAKER_COOLDOWN_MINUTES（默认 10 分钟）；
  - 其他错误：连续 LLM_BREAKER_FAILURES（默认 3）次才冷却 10 分钟；
  - 冷却到期后放一次请求试探（半开），成功就恢复，失败按上面重新冷却；
  - 进入冷却、恢复都记审计（MODEL_CIRCUIT_OPEN / MODEL_CIRCUIT_CLOSED）并发飞书告警（alerts，默认关）；
  - 链上全部不可用：抛 ChainUnavailable，调用方按原来的逻辑转人工。
格式错误（模型回了但 JSON 不对）不算模型故障，不计入熔断。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ..config import Settings

log = logging.getLogger(__name__)

KINDS = ("chat", "translate", "vision", "light")
QUOTA_PATTERNS = re.compile(r"FreeTierOnly|free tier|quota|Arrearage|insufficient[_ ]?(quota|balance)|AllocationQuota|"
                            r"额度|欠费|余额不足|exceeded your current quota|billing", re.I)
TIMEOUT_NAMES = ("Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout", "APITimeoutError")


class ChainUnavailable(RuntimeError):
    """链上的模型都不可用（都在冷却，或这次都失败了）。"""


@dataclass
class ModelSpec:
    name: str
    extra_body: dict | None = None
    api_key: str | None = None
    base_url: str | None = None


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").replace("，", ",").split(",") if x.strip()]


def model_options(s: Settings) -> dict[str, dict]:
    if not s.llm_model_options:
        return {}
    try:
        v = json.loads(s.llm_model_options)
    except json.JSONDecodeError as e:
        log.warning("LLM_MODEL_OPTIONS 不是合法 JSON：%s", e)
        return {}
    return v if isinstance(v, dict) else {}


def chain_names(s: Settings, kind: str) -> list[str]:
    """一条链上的模型名（去重，保持顺序）。"""
    if kind == "chat":
        names = _split(s.llm_chat_models) or _split(s.llm_chat_model)
    elif kind == "translate":
        names = _split(s.llm_translate_models) or _split(s.llm_translate_model) or chain_names(s, "chat")
    elif kind == "vision":
        names = _split(s.vision_models) or _split(s.vision_model)
    elif kind == "light":
        names = _split(s.llm_light_models) or chain_names(s, "chat")
    else:
        raise ValueError(kind)
    return list(dict.fromkeys(names))


def chain_specs(s: Settings, kind: str) -> list[ModelSpec]:
    """一条链上每个模型的参数：LLM_MODEL_OPTIONS 里写了的用它的，没写的沿用全局配置。"""
    opts = model_options(s)
    out = []
    for name in chain_names(s, kind):
        o = opts.get(name) or {}
        if kind == "vision":
            default_extra, key, base = None, s.vision_api_key, s.vision_base_url
        elif kind == "translate":
            default_extra, key, base = s.translate_extra_body() if name == s.translate_model_name else _thinking_off(s, name), \
                s.llm_api_key, s.llm_base_url
        else:
            default_extra, key, base = s.extra_body(), s.llm_api_key, s.llm_base_url
        extra = o.get("extra_body") if "extra_body" in o else default_extra
        out.append(ModelSpec(name=name, extra_body=extra or None, api_key=o.get("api_key") or key,
                             base_url=o.get("base_url") or base))
    return out


def _thinking_off(s: Settings, name: str) -> dict | None:
    """翻译链上的其他模型：qwen3 系列按 LLM_TRANSLATE_THINKING 开关思考，其余透传全局 extra_body。"""
    body = dict(s.extra_body() or {})
    if "enable_thinking" in body or name.startswith("qwen3"):
        body["enable_thinking"] = bool(s.llm_translate_thinking)
    return body or None


# ------------------------------------------------------------------ 熔断
@dataclass
class Health:
    fails: int = 0
    open_until: datetime | None = None
    reason: str | None = None
    kind: str | None = None  # quota / timeout / error
    last_error: str | None = None
    last_ok_at: datetime | None = None
    last_fail_at: datetime | None = None
    opened_at: datetime | None = None
    probing: bool = False
    calls: int = 0
    failures: int = 0
    history: list = field(default_factory=list)


def classify(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    if QUOTA_PATTERNS.search(text) or (status == 403 and "free" in text.lower()):
        return "quota"
    if any(n in type(e).__name__ for n in TIMEOUT_NAMES) or "timed out" in text.lower() or "timeout" in text.lower():
        return "timeout"
    return "error"


class Breaker:
    def __init__(self):
        self._lock = threading.Lock()
        self.models: dict[str, Health] = {}
        self.clock: Callable[[], datetime] | None = None  # 测试可以换成假时钟

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        from ..db import utcnow

        return utcnow()

    def _h(self, name: str) -> Health:
        return self.models.setdefault(name, Health())

    def available(self, name: str) -> bool:
        """不在冷却，或冷却到期（放一次试探）。"""
        with self._lock:
            h = self._h(name)
            if h.open_until is None:
                return True
            if self.now() >= h.open_until and not h.probing:
                h.probing = True  # 半开：放这一个请求去试
                return True
            return False

    def success(self, name: str) -> None:
        with self._lock:
            h = self._h(name)
            was_open = h.open_until is not None
            h.fails, h.open_until, h.reason, h.kind, h.probing = 0, None, None, None, False
            h.last_ok_at = self.now()
            h.calls += 1
        if was_open:
            _event("MODEL_CIRCUIT_CLOSED", name, "恢复：试探调用成功，重新使用", level="ok")

    def failure(self, name: str, e: BaseException, s: Settings) -> str:
        kind = classify(e)
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        opened = None
        with self._lock:
            h = self._h(name)
            h.calls += 1
            h.failures += 1
            h.last_error, h.last_fail_at = msg, self.now()
            h.fails += 1
            probing = h.probing
            h.probing = False
            if kind == "quota":
                cool = timedelta(hours=max(0, s.llm_breaker_quota_hours))
            elif kind == "timeout" or probing or h.fails >= max(1, s.llm_breaker_failures):
                cool = timedelta(minutes=max(0, s.llm_breaker_cooldown_minutes))
            else:
                cool = None
            if cool is not None:
                h.open_until, h.reason, h.kind, h.opened_at = self.now() + cool, msg, kind, self.now()
                opened = cool
        if opened is not None:
            why = {"quota": "额度类错误", "timeout": "超时"}.get(kind, f"连续失败 {s.llm_breaker_failures} 次" if not probing else "试探失败")
            _event("MODEL_CIRCUIT_OPEN", name, f"{why}，冷却 {_dur(opened)}，自动切到链上下一个模型。错误：{msg}",
                   level="error" if kind == "quota" else "warning", detail={"kind": kind, "cooldown_seconds": int(opened.total_seconds())})
        return kind

    def snapshot(self) -> list[dict]:
        now = self.now()
        with self._lock:
            return [{"model": n, "state": "open" if h.open_until and now < h.open_until else ("half_open" if h.open_until else "closed"),
                     "open_until": h.open_until.isoformat(timespec="seconds") + "Z" if h.open_until else None,
                     "reason": h.reason, "kind": h.kind, "consecutive_failures": h.fails, "calls": h.calls, "failures": h.failures,
                     "last_error": h.last_error,
                     "last_ok_at": h.last_ok_at.isoformat(timespec="seconds") + "Z" if h.last_ok_at else None}
                    for n, h in sorted(self.models.items())]

    def reset(self) -> None:
        with self._lock:
            self.models.clear()


def _dur(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    return f"{m // 60} 小时" if m >= 60 and m % 60 == 0 else f"{m} 分钟"


def _event(action: str, model: str, text: str, level: str = "warning", detail: dict | None = None) -> None:
    """熔断 / 恢复：审计（独立事务）+ 飞书告警（默认关）。失败只记日志。"""
    try:
        from ..db import session_scope
        from ..models import AuditEvent

        with session_scope() as db:
            db.add(AuditEvent(actor="model_router", action=action, detail={"model": model, "text": text[:500], **(detail or {})}))
    except Exception:  # noqa: BLE001
        log.exception("记模型熔断审计失败")
    try:
        from .. import alerts

        title = f"模型{'恢复' if action.endswith('CLOSED') else '熔断'}：{model}"
        alerts.notify("model", title, text, level=level, key=f"model:{action}:{model}")
    except Exception:  # noqa: BLE001
        log.exception("模型熔断告警失败")


breaker = Breaker()


def run_chain(specs: list[ModelSpec], call: Callable[[ModelSpec], Any], s: Settings, *, what: str = "调用") -> tuple[Any, str]:
    """按顺序在链上调用：跳过冷却中的模型；异常计入熔断后试下一个。返回 (结果, 实际用的模型名)。"""
    if not specs:
        raise ChainUnavailable("没有配置模型")
    errors = []
    tried = False
    for spec in specs:
        if not breaker.available(spec.name):
            errors.append(f"{spec.name}：冷却中")
            continue
        tried = True
        try:
            out = call(spec)
        except Exception as e:  # noqa: BLE001  模型故障：记熔断，切下一个
            kind = breaker.failure(spec.name, e, s)
            errors.append(f"{spec.name}：{kind} {type(e).__name__}: {str(e)[:160]}")
            log.warning("模型 %s %s失败（%s），试链上下一个：%s", spec.name, what, kind, e)
            continue
        breaker.success(spec.name)
        return out, spec.name
    raise ChainUnavailable(("链上模型都不可用：" if not tried else "链上模型都失败了：") + "；".join(errors))
