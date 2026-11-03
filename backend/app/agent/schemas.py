"""Agent 的内部契约：可信运行上下文、统一工具返回包、模型草稿输出（第 11.2、12.2、14.5 节）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..intents import INTENTS

IntentCode = Literal[tuple(INTENTS.keys())]  # type: ignore[valid-type]
ToolStatus = Literal["FOUND", "NOT_FOUND", "AMBIGUOUS", "STALE", "ERROR"]


@dataclass(frozen=True)
class RuntimeContext:
    """服务端鉴权后注入的运行身份，模型不能填写或改写（第 14.5 节）。"""

    store_id: str
    conversation_id: int
    buyer_key: str
    marketplace_id: str
    item_id: str | None
    job_id: int
    run_generation: int

    @property
    def thread_id(self) -> str:
        return (
            f"store:{self.store_id}:conversation:{self.conversation_id}"
            f":job:{self.job_id}:generation:{self.run_generation}"
        )


class ToolResult(BaseModel):
    """统一返回包。查询失败必须是 ERROR，不能伪装成 NOT_FOUND。"""

    tool_name: str
    request_id: str
    status: ToolStatus
    data: Any = None
    source: str | None = None
    queried_at: str
    evidence_ref: str | None = None
    message: str | None = None  # 给模型看的简短状态说明，不含 SQL/内部错误


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)


class ReplyDraftOutput(BaseModel):
    """模型输出契约。risk_level 不让模型填：风险等级由程序按第 3.2 节决定。"""

    model_config = ConfigDict(extra="forbid")

    intent: IntentCode
    intents: list[IntentCode] = Field(min_length=1, max_length=5)
    language: str = Field(max_length=16, description="回复语言代码，如 en、de、zh")
    action: Literal["answer", "clarify", "handoff"]
    needs_human: bool
    claims: list[Claim] = Field(default_factory=list, max_length=12)
    missing_facts: list[str] = Field(default_factory=list, max_length=10)
    conflicts: list[str] = Field(default_factory=list, max_length=10, description="相互冲突的证据 ID")
    # 放在 reply_text 之前：先理清思路再写回复。只给审核员看，不进入发给买家的内容
    reasoning_zh: str = Field(default="", max_length=600, description="给审核员看的中文回复思路，2–4 句，不超过 200 字")
    reply_text: str = Field(max_length=3000)
    reply_zh: str = Field(default="", max_length=3000, description="reply_text 的中文回译，仅供审核员")
    reason_code: str = Field(max_length=64)

    @field_validator("intents")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))
