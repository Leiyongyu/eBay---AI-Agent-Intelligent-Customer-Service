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
    return {k: meta.get(k) for k in ("input_tokens", "output_tokens", "total_tokens") if meta.get(k) is not None}


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
    def __init__(self, settings: Settings, model: BaseChatModel | None = None):
        self.settings = settings
        self.model = model or self._build(settings)
        # 翻译用的模型：配置了 LLM_TRANSLATE_MODEL 或要关思考时单独建一个；测试注入了假模型时沿用它
        self._translator: BaseChatModel | None = model

    @property
    def translator(self) -> BaseChatModel:
        if self._translator is None:
            s = self.settings
            same = s.translate_model_name == s.llm_chat_model and s.translate_extra_body() == s.extra_body()
            self._translator = self.model if same else self._build(s, model=s.translate_model_name,
                                                                   extra=s.translate_extra_body())
        return self._translator

    @staticmethod
    def _build(s: Settings, model: str | None = None, extra: dict | None = None) -> BaseChatModel:
        if s.llm_provider != "openai_compatible":
            raise ProviderNotReady(f"LLM_PROVIDER={s.llm_provider} 暂不支持")
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": model or s.llm_chat_model,
            "api_key": s.llm_api_key,
            "timeout": s.llm_timeout_seconds,
            "max_retries": 1,
        }
        if s.llm_base_url:
            kwargs["base_url"] = s.llm_base_url
        if s.llm_max_output_tokens:
            kwargs["max_tokens"] = s.llm_max_output_tokens
        if s.llm_temperature is not None:
            kwargs["temperature"] = s.llm_temperature
        body = extra if model else s.extra_body()
        if body:
            kwargs["extra_body"] = body
        return ChatOpenAI(**kwargs)

    # ------------------------------------------------------------------
    def structured(self, messages: list[BaseMessage], schema: type[BaseModel]) -> StructuredResult:
        method = self.settings.llm_structured_output
        if method in ("json_schema", "function_calling"):
            runnable = self.model.with_structured_output(schema, method=method, include_raw=True)
            out = runnable.invoke(messages)
            raw = out.get("raw")
            raw_text = getattr(raw, "content", "") or json.dumps(getattr(raw, "tool_calls", []), ensure_ascii=False)
            err = out.get("parsing_error")
            return StructuredResult(out.get("parsed"), str(raw_text), str(err) if err else None, _usage(raw))

        msgs = list(messages)
        msgs[0] = SystemMessage(content=f"{msgs[0].content}\n\n{schema_instruction(schema)}")
        model = self.model.bind(response_format={"type": "json_object"}) if method == "json_mode" else self.model
        resp = model.invoke(msgs)
        return self.parse(str(resp.content), schema, _usage(resp))

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
        resp = self.model.bind_tools(tools).invoke(messages)
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
        resp = self.translator.invoke([SystemMessage(content=system), HumanMessage(content=text)])
        return str(resp.content).strip(), _usage(resp)

    def ping(self) -> dict:
        resp = self.model.invoke([HumanMessage(content='只回复 JSON：{"ok": true}')])
        return {"content": str(resp.content)[:200], "usage": _usage(resp)}


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
