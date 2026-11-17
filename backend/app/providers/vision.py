"""VisionModelProvider：图片观察（第 15.1、20 章）。

视觉配置（VISION_*）与聊天模型完全独立；VISION_ENABLED=false 或配置不全时 get_vision_provider() 返回 None，
由调用方把附件交给人工，不静默跳过、不返回模拟结果。输出只是“观察”，不作为退款、质量或适配的结论。
"""
from __future__ import annotations

import base64
import re
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, get_settings
from ..prompts import load_prompt, prompt_version
from .chat import ChatModelProvider, StructuredResult, _usage, schema_instruction

PROMPT_NAME = "vision_observe"


class ImageObservationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_kind: Literal["产品照片", "安装位置", "故障码或仪表屏幕", "包装外观", "快递面单", "车辆铭牌或VIN",
                        "发票或单据", "聊天或网页截图", "其他"]
    quality: Literal["清晰", "模糊", "关键部位看不到"]
    description: str = Field(max_length=300)
    visible_text: list[str] = Field(default_factory=list, max_length=20)
    part_numbers: list[str] = Field(default_factory=list, max_length=10)
    error_codes: list[str] = Field(default_factory=list, max_length=10)
    vin_masked: str = Field(default="", max_length=20)
    damage_observed: bool = False
    damage_description: str = Field(default="", max_length=300)
    uncertain: list[str] = Field(default_factory=list, max_length=10)
    contains_personal_info: bool = False


# 模型偶尔不守规矩时的兜底：完整 VIN、邮箱、电话在入库前再打码一次
_VIN = re.compile(r"(?<![A-HJ-NPR-Z0-9])([A-HJ-NPR-Z0-9]{11})[A-HJ-NPR-Z0-9]{6}(?![A-HJ-NPR-Z0-9])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+\d[\d ()\-]{8,}\d")


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        value = _VIN.sub(lambda m: m.group(1) + "******" if re.search(r"\d", m.group()) and re.search(r"[A-Z]", m.group())
                         else m.group(), value)
        return _PHONE.sub("[电话]", _EMAIL.sub("[邮箱]", value))
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


class VisionModelProvider:
    """识图：走识图模型链（VISION_MODELS，空 = VISION_MODEL），经熔断器按顺序选可用的模型；测试注入假模型时只用它。"""

    def __init__(self, settings: Settings, model: BaseChatModel | None = None):
        self.settings = settings
        self._injected = model
        self._models: dict[str, BaseChatModel] = {}
        self.last_model: str | None = None

    @property
    def model(self) -> BaseChatModel:
        if self._injected is not None:
            return self._injected
        from .model_chain import chain_specs

        return self._model_for(chain_specs(self.settings, "vision")[0])

    def _model_for(self, spec) -> BaseChatModel:
        if spec.name not in self._models:
            self._models[spec.name] = self._build(self.settings, spec)
        return self._models[spec.name]

    @staticmethod
    def _build(s: Settings, spec=None) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {"model": spec.name if spec else s.vision_model,
                                  "api_key": (spec.api_key if spec else None) or s.vision_api_key,
                                  "timeout": s.vision_timeout_seconds, "max_retries": 0, "max_tokens": 1000}
        base = (spec.base_url if spec else None) or s.vision_base_url
        if base:
            kwargs["base_url"] = base
        if spec is not None and spec.extra_body:
            kwargs["extra_body"] = spec.extra_body
        return ChatOpenAI(**kwargs)

    def _invoke(self, messages):
        if self._injected is not None:
            self.last_model = self.settings.vision_model or "injected"
            return self._injected.invoke(messages)
        from .model_chain import chain_specs, run_chain

        resp, self.last_model = run_chain(chain_specs(self.settings, "vision"),
                                          lambda spec: self._model_for(spec).invoke(messages), self.settings, what="识图")
        return resp

    @property
    def config_version(self) -> str:
        return self.settings.vision_config_version()

    @property
    def prompt_version(self) -> str:
        return prompt_version(PROMPT_NAME)

    def observe(self, image: bytes, mime: str, context: str = "") -> StructuredResult:
        """context 只放刊登标题、SKU 这类商品背景，不放买家个人信息。"""
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        messages = [
            SystemMessage(content=f"{load_prompt(PROMPT_NAME)}\n\n{schema_instruction(ImageObservationOutput)}"),
            HumanMessage(content=[
                {"type": "text", "text": f"商品背景：{context or '未知'}\n请观察这张买家图片。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]),
        ]
        resp = self._invoke(messages)
        result = ChatModelProvider.parse(str(resp.content), ImageObservationOutput, _usage(resp))
        if result.parsed is not None:
            result.parsed = ImageObservationOutput.model_validate(scrub(result.parsed.model_dump()))
        return result


_override: VisionModelProvider | None = None


def set_vision_provider_override(provider: VisionModelProvider | None) -> None:
    """测试用：注入假视觉模型。"""
    global _override
    _override = provider


def get_vision_provider() -> VisionModelProvider | None:
    if _override is not None:
        return _override
    s = get_settings()
    if s.vision_not_ready_reasons():
        return None
    return VisionModelProvider(s)
