"""AgentState（可被节点更新的运行状态）与 AgentDeps（服务端注入、模型不可改写的上下文与依赖）。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TypedDict

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Store
from ..providers.chat import ChatModelProvider
from ..providers.vision import VisionModelProvider
from ..sources import BusinessSource
from .schemas import RuntimeContext


class AgentState(TypedDict, total=False):
    snapshot: dict  # 会话快照：row_version、本次覆盖的买家消息
    memory: dict  # 最近原文
    observations: list[dict]  # ToolResult
    attachments: list[dict]  # 本次任务买家消息的附件：处理状态 + 图片观察（证据 ID 形如 img:附件ID）
    knowledge: dict  # 全量注入的条款与 token 估算
    problems: list[str]  # 原因码（见 guards/risk.py REASON_LABELS）
    skip_generation: bool
    raw_output: str
    parsed: dict | None
    parse_error: str | None
    repairs: int
    usage: dict
    route: str
    result: dict  # 最终候选：草稿字段 + 闸门决定


@dataclass
class AgentDeps:
    run: RuntimeContext
    db: Session
    settings: Settings
    source: BusinessSource
    llm: ChatModelProvider | None
    store: Store
    run_id: int
    deadline: float  # time.monotonic() 截止点
    vision: VisionModelProvider | None = None  # 未开启或未配置时为 None，附件交人工查看

    def time_left(self) -> float:
        return self.deadline - time.monotonic()
