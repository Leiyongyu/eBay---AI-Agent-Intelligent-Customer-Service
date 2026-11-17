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


def _cut(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


# 状态卡里的一个未解决问题（模型写；系统问题由程序写，模型不用重复）。类上不写 docstring：会进输出结构、多花 token
class StateIssue(BaseModel):

    model_config = ConfigDict(extra="ignore")

    intent: str
    summary_zh: str = ""
    order_id: str | None = None
    status_zh: str = ""

    @field_validator("intent")
    @classmethod
    def _code(cls, v: str) -> str:
        return v if v in INTENTS else "other"  # 写错了不让整份输出作废（省一次修复调用）

    @field_validator("summary_zh", "status_zh")
    @classmethod
    def _cap(cls, v: str) -> str:
        return _cut(v, 40)


class StateFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_ids: list[str] = Field(default_factory=list)
    vin: str | None = None
    vehicle: str | None = None
    skus: list[str] = Field(default_factory=list)

    @field_validator("order_ids", "skus")
    @classmethod
    def _few(cls, v: list[str]) -> list[str]:
        return [_cut(x, 40) for x in v if x][:5]


# 模型在起草时顺带给出的状态卡更新（同一次调用，不另外请求）
class StateUpdate(BaseModel):

    model_config = ConfigDict(extra="ignore")

    # 字段说明写在提示词里（reply.md），这里不加 description，省输出结构的 token
    topic: str = ""
    open_issues: list[StateIssue] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)
    facts: StateFacts = Field(default_factory=StateFacts)
    promises: list[str] | None = None

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        return _cut(v, 30)

    @field_validator("open_issues")
    @classmethod
    def _issues(cls, v: list) -> list:
        return v[:6]

    @field_validator("resolved")
    @classmethod
    def _resolved(cls, v: list[str]) -> list[str]:
        return [_cut(x, 40) for x in v if x][:4]

    @field_validator("promises")
    @classmethod
    def _promises(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [_cut(x, 40) for x in v if x][:4]


class ReplyDraftOutput(BaseModel):
    """模型输出契约。risk_level 不让模型填：风险等级由程序按第 3.2 节决定。"""

    model_config = ConfigDict(extra="forbid")

    intent: IntentCode
    intents: list[IntentCode] = Field(min_length=1, max_length=5)
    # 意图把握度和依据：按整段会话 + 状态卡判断；low 时不硬猜（action 用 clarify，程序加审核原因 INTENT_UNCERTAIN）
    intent_confidence: Literal["high", "medium", "low"] | None = None  # high 明确 / medium 结合上下文 / low 看不出指哪件事
    intent_basis: list[str] = Field(default_factory=list)  # 判断意图依据的消息 ID
    inherits_issue: str | None = None  # 沿用的状态卡未解决问题 id；新话题为 null
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
    # 会话情况标签：客服在列表里一眼看出这个会话怎么了（看整段会话，不只是最新一条）
    situation_stage: Literal["presale", "shipping", "aftersales", "invoice", "other"] = Field(
        default="other", description="会话现在所处阶段：presale 售前咨询 / shipping 物流跟进 / aftersales 售后处理 / invoice 发票 / other 其他")
    situation_zh: str = Field(default="", description="一句中文概括这个会话现在是什么情况、卡在哪，不超过 16 个汉字")
    state_update: StateUpdate | None = None  # 这次回复后的会话状态卡更新（说明见 reply.md）
    reason_code: str = Field(max_length=64)

    @field_validator("intent_basis")
    @classmethod
    def _basis(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(_cut(x, 128) for x in v if x))[:10]

    @field_validator("intent_confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        """清单外的值（如 "very high"、"高"）不让整份输出作废、多修复一次：能认出来的归一，认不出当 null。"""
        if v is None:
            return None
        t = str(v).strip().lower()
        return {"high": "high", "高": "high", "medium": "medium", "mid": "medium", "中": "medium",
                "low": "low", "低": "low"}.get(t)

    @field_validator("inherits_issue")
    @classmethod
    def _inherits(cls, v: str | None) -> str | None:
        v = _cut(v, 80)
        return v if v and v.lower() not in ("null", "none") else None

    @field_validator("situation_zh")
    @classmethod
    def _short(cls, v: str) -> str:
        return " ".join((v or "").split())[:24]

    @field_validator("intents")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))
