"""ChatModelProvider：按人工配置构造 LangChain 聊天模型，并统一结构化输出（第 15.1 节）。

不把具体模型名写进代码；配置缺失时 get_chat_provider() 返回 None，由调用方显示 AGENT_NOT_READY，
绝不静默选择默认模型或返回模拟结果。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from ..config import Settings, get_settings
from ..prompts import load_prompt


class ProviderNotReady(RuntimeError):
    pass


@dataclass
class StructuredResult:
    parsed: BaseModel | None
    raw_text: str
    error: str | None = None
    usage: dict = field(default_factory=dict)


def _usage(msg: Any) -> dict:
    meta = getattr(msg, "usage_metadata", None) or {}
    out = {k: meta.get(k) for k in ("input_tokens", "output_tokens", "total_tokens") if meta.get(k) is not None}
    cached = (meta.get("input_token_details") or {}).get("cache_read")  # 前缀缓存命中的输入 token（百炼 cached_tokens）
    if cached:
        out["cached_tokens"] = cached
    return out


def _merge_usage(a: dict, b: dict) -> dict:
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in set(a) | set(b)}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> Any:
    """从模型文本中取出 JSON 对象：兼容 ```json 代码块和前后多余文字。"""
    t = _FENCE.sub("", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start: end + 1])
        raise


def _no_titles(node):
    """去掉 Pydantic 自动生成的 title（字段名的英文重复），省提示词 token，不影响约束。"""
    if isinstance(node, dict):
        return {k: _no_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_no_titles(v) for v in node]
    return node


def schema_instruction(schema: type[BaseModel]) -> str:
    return (
        "只输出一个 JSON 对象，不要输出 Markdown 代码块或任何解释文字。"
        "JSON 必须符合以下 JSON Schema（不要输出额外字段）：\n"
        + json.dumps(_no_titles(schema.model_json_schema()), ensure_ascii=False, separators=(",", ":"))
    )


class ChatModelProvider:
    """聊天模型：回复起草走模型链（LLM_CHAT_MODELS，轻量档走 LLM_LIGHT_MODELS），翻译走翻译链；每次调用经熔断器
    （model_chain.run_chain）按顺序选可用的模型，失败 / 额度用完自动切下一个。测试注入假模型时只用它、不走熔断。"""

    def __init__(self, settings: Settings, model: BaseChatModel | None = None, *, tier: str = "main"):
        self.settings = settings
        self.tier = tier
        self._injected = model
        self._models: dict[tuple[str, str], BaseChatModel] = {}
        self.last_model: str | None = None  # 最近一次调用实际用的模型（用量统计、草稿依据）
        if model is None and settings.llm_provider != "openai_compatible":
            raise ProviderNotReady(f"LLM_PROVIDER={settings.llm_provider} 暂不支持")

    def for_tier(self, tier: str) -> "ChatModelProvider":
        """同一配置换一个档位（light 轻量档 / main 主力档）。"""
        if tier == self.tier:
            return self
        p = ChatModelProvider(self.settings, self._injected, tier=tier)
        p._models = self._models
        return p

    # ---- 模型链 ----
    def _kind(self, kind: str) -> str:
        return "light" if (kind == "chat" and self.tier == "light") else kind

    def specs(self, kind: str = "chat"):
        from .model_chain import chain_specs

        return chain_specs(self.settings, self._kind(kind))

    def _model_for(self, spec, kind: str) -> BaseChatModel:
        key = (kind, spec.name)
        if key not in self._models:
            self._models[key] = self._build(self.settings, spec)
        return self._models[key]

    def _run(self, fn, kind: str = "chat", what: str = "调用"):
        """在链上调用 fn(model)；返回 fn 的结果，记下实际用的模型。"""
        if self._injected is not None:
            self.last_model = getattr(self._injected, "model_name", None) or self.settings.llm_chat_model or "injected"
            return fn(self._injected)
        from .model_chain import run_chain

        out, name = run_chain(self.specs(kind), lambda spec: fn(self._model_for(spec, kind)), self.settings, what=what)
        self.last_model = name
        return out

    @property
    def model(self) -> BaseChatModel:
        """链上第一个模型（判断能力、兼容旧代码用；真正调用走 _run）。"""
        if self._injected is not None:
            return self._injected
        specs = self.specs("chat")
        if not specs:
            raise ProviderNotReady("没有配置聊天模型")
        return self._model_for(specs[0], "chat")

    @property
    def translator(self) -> BaseChatModel:
        if self._injected is not None:
            return self._injected
        return self._model_for(self.specs("translate")[0], "translate")

    @staticmethod
    def _build(s: Settings, spec=None, model: str | None = None, extra: dict | None = None) -> BaseChatModel:
        if s.llm_provider != "openai_compatible":
            raise ProviderNotReady(f"LLM_PROVIDER={s.llm_provider} 暂不支持")
        from langchain_openai import ChatOpenAI

        name = spec.name if spec is not None else (model or s.llm_chat_model)
        kwargs: dict[str, Any] = {
            "model": name,
            "api_key": (spec.api_key if spec is not None else None) or s.llm_api_key,
            "timeout": s.llm_timeout_seconds,
            "max_retries": 0,  # 不在 SDK 里重试：失败直接交给熔断器切下一个模型
        }
        base = (spec.base_url if spec is not None else None) or s.llm_base_url
        if base:
            kwargs["base_url"] = base
        if s.llm_max_output_tokens:
            kwargs["max_tokens"] = s.llm_max_output_tokens
        if s.llm_temperature is not None:
            kwargs["temperature"] = s.llm_temperature
        body = spec.extra_body if spec is not None else (extra if model else s.extra_body())
        if body:
            kwargs["extra_body"] = body
        return ChatOpenAI(**kwargs)

    # ---- Prompt 前缀缓存（百炼显式缓存）：系统提示 + 店铺知识在最前，带 cache_control ----
    def _cache(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        if not self.settings.llm_prompt_cache or not messages or not isinstance(messages[0], SystemMessage):
            return messages
        first = messages[0]
        if isinstance(first.content, list):
            return messages
        return [SystemMessage(content=[{"type": "text", "text": first.content, "cache_control": {"type": "ephemeral"}}]),
                *messages[1:]]

    # ------------------------------------------------------------------
    def structured(self, messages: list[BaseMessage], schema: type[BaseModel]) -> StructuredResult:
        method = self.settings.llm_structured_output
        if method in ("json_schema", "function_calling"):
            def call_schema(model):
                runnable = model.with_structured_output(schema, method=method, include_raw=True)
                out = runnable.invoke(self._cache(messages))
                raw = out.get("raw")
                raw_text = getattr(raw, "content", "") or json.dumps(getattr(raw, "tool_calls", []), ensure_ascii=False)
                err = out.get("parsing_error")
                return StructuredResult(out.get("parsed"), str(raw_text), str(err) if err else None, _usage(raw))
            return self._run(call_schema, what="起草")

        msgs = list(messages)
        msgs[0] = SystemMessage(content=f"{msgs[0].content}\n\n{schema_instruction(schema)}")
        msgs = self._cache(msgs)

        def call(model):
            m = model.bind(response_format={"type": "json_object"}) if method == "json_mode" else model
            resp = m.invoke(msgs)
            return self.parse(str(resp.content), schema, _usage(resp))
        return self._run(call, what="起草")

    # ------------------------------------------------------------------ 原生工具调用
    @property
    def supports_tools(self) -> bool:
        """模型是否支持原生工具调用（OpenAI 兼容接口支持；测试用的简单假模型不支持时走原来的单次生成）。"""
        return type(self.model).bind_tools is not BaseChatModel.bind_tools

    @staticmethod
    def with_answer_schema(messages: list[BaseMessage], schema: type[BaseModel]) -> list[BaseMessage]:
        """工具调用对话里，最终作答仍按输出契约给 JSON：把 Schema 要求附在系统提示后面。"""
        msgs = list(messages)
        msgs[0] = SystemMessage(content=f"{msgs[0].content}\n\n不需要再查询时，{schema_instruction(schema)}")
        return msgs

    def tool_round(self, messages: list[BaseMessage], tools: list[dict]) -> tuple[AIMessage, dict]:
        """一轮：模型要么返回 tool_calls（要查什么），要么直接给出最终 JSON。"""
        resp = self._run(lambda model: model.bind_tools(tools).invoke(self._cache(messages)), what="工具调用")
        return resp, _usage(resp)

    def repair(self, messages: list[BaseMessage], schema: type[BaseModel], bad_output: str, error: str,
               usage: dict) -> StructuredResult:
        """仅修复一次候选输出的格式（第 14.3 节），不改变事实内容。"""
        fix = HumanMessage(content=(
            "上一次输出不符合 Schema，错误如下。请只修正格式，保留原有含义，重新输出完整 JSON。\n"
            f"<error>{error[:1500]}</error>\n<previous_output>{bad_output[:6000]}</previous_output>"
        ))
        result = self.structured([*messages, fix], schema)
        result.usage = _merge_usage(usage, result.usage)
        return result

    @staticmethod
    def parse(text: str, schema: type[BaseModel], usage: dict | None = None) -> StructuredResult:
        try:
            return StructuredResult(schema.model_validate(extract_json(text)), text, None, usage or {})
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            return StructuredResult(None, text, f"{type(e).__name__}: {e}", usage or {})

    def translate(self, text: str, target_language: str = "中文") -> str:
        return self.translate_with_usage(text, target_language)[0]

    def translate_with_usage(self, text: str, target_language: str = "中文") -> tuple[str, dict]:
        system = load_prompt("translate_review").replace("{target_language}", target_language)
        resp = self._run(lambda model: model.invoke([SystemMessage(content=system), HumanMessage(content=text)]),
                         kind="translate", what="翻译")
        return str(resp.content).strip(), _usage(resp)

    def ping(self) -> dict:
        resp = self._run(lambda model: model.invoke([HumanMessage(content='只回复 JSON：{"ok": true}')]), what="连通测试")
        return {"content": str(resp.content)[:200], "usage": _usage(resp), "model": self.last_model}


_override: ChatModelProvider | None = None


def set_chat_provider_override(provider: ChatModelProvider | None) -> None:
    """测试用：注入假模型。"""
    global _override
    _override = provider


def get_chat_provider() -> ChatModelProvider | None:
    if _override is not None:
        return _override
    s = get_settings()
    if s.agent_not_ready_reasons():
        return None
    return ChatModelProvider(s)
