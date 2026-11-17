"""给起草模型套一层个人信息脱敏（第二批 B7）：发给模型的每条消息先脱敏（姓名、地址、邮箱、电话换成 <NAME_1> 等），
模型返回的结构化输出（回复正文、中文回译、claims、思路、情况概括、状态卡更新）再还原成原值；原始输出（raw_text）保持脱敏，
修复重试时会原样发回给模型。节点逻辑不用改：compose_reply / repair_output 把 llm 换成 MaskedLLM 即可。
同一次运行里 compose 和 repair 各自按同样的消息建 PiiMasker，占位符编号一致（按出现顺序编号，已知值来自同一份工具结果）。
"""
from __future__ import annotations

from ..guards.pii import PiiMasker, known_pii


def masker_for(state: dict, buyer_key: str | None = None) -> PiiMasker:
    """本次起草的脱敏器：已知值取工具结果（订单、收件信息）里的姓名、地址、电话、邮箱。买家 eBay 用户名不脱敏（打招呼要用）。"""
    known = known_pii([o.get("data") for o in state.get("observations") or []])
    return PiiMasker(known=known)


def _restore_obj(m: PiiMasker, obj):
    if isinstance(obj, str):
        return m.restore(obj)
    if isinstance(obj, list):
        return [_restore_obj(m, x) for x in obj]
    if isinstance(obj, dict):
        return {k: _restore_obj(m, v) for k, v in obj.items()}
    return obj


class MaskedLLM:
    """包住 ChatModelProvider：structured / tool_round / parse / repair 前后脱敏、还原，其余属性原样转发。"""

    def __init__(self, llm, masker: PiiMasker):
        self._llm = llm
        self.masker = masker

    def __getattr__(self, name):
        return getattr(self._llm, name)

    def for_tier(self, tier: str) -> "MaskedLLM":
        return MaskedLLM(self._llm.for_tier(tier) if hasattr(self._llm, "for_tier") else self._llm, self.masker)

    def _mask_msgs(self, messages: list) -> list:
        out = []
        for msg in messages:
            if isinstance(getattr(msg, "content", None), str):
                msg = msg.model_copy(update={"content": self.masker.mask(msg.content)})
            out.append(msg)
        return out

    def _restore(self, res):
        if res is None or getattr(res, "parsed", None) is None:
            return res
        data = _restore_obj(self.masker, res.parsed.model_dump())
        schema = type(res.parsed)
        try:
            res.parsed = schema.model_validate(data)
        except Exception:  # noqa: BLE001  还原后超长等：不再校验，原样放回（后面的校验会拦）
            res.parsed = schema.model_construct(**data)
        return res

    def structured(self, messages, schema):
        return self._restore(self._llm.structured(self._mask_msgs(messages), schema))

    def tool_round(self, messages, tools):
        return self._llm.tool_round(self._mask_msgs(messages), tools)

    def parse(self, text, schema, usage=None):
        return self._restore(self._llm.parse(text, schema, usage))

    def repair(self, messages, schema, bad_output, error, usage=None):
        return self._restore(self._llm.repair(self._mask_msgs(messages), schema, bad_output, error, usage))

    def pii_summary(self) -> dict:
        """这次脱敏了哪几类、各几个（只记数量，不记原值）。"""
        out: dict[str, int] = {}
        for ph in self.masker.mapping:
            kind = ph[1:].split("_")[0]
            out[kind] = out.get(kind, 0) + 1
        return out
