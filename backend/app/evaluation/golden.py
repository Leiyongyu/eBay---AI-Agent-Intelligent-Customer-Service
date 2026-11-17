"""黄金回归集的格式（一行一个 JSON，data/eval/golden.jsonl；CI 用 tests/fixtures/golden_sample.jsonl）。

{
  "case_id": "G-ab12cd34",                  # 稳定 ID（来源 + 原始 ID 的哈希），重抽不变
  "source": "agent" | "manual" | "baseline" | "synthetic",
  "store": "S1",                            # 店铺别名（不写真实店铺 ID / 名称）
  "marketplace": "EBAY_DE", "language": "de", "stratum": "shipping_status",
  "input": {
    "title": "...",                         # 会话标题（多为刊登标题）
    "history": [{"role": "BUYER" | "SELLER", "text": "...", "ago": "2d"}],   # 本次之前的对话
    "inbound": [{"text": "...", "ago": "0m"}],                              # 本次要回复的买家消息（最后几条）
    "tools":   [{"tool_name", "arguments", "status", "data", "evidence_ref", "message"}],  # 当时的工具结果（已脱敏）
    "knowledge": [{"clause_id", "version", "title", "text"}],              # 当时命中 / 引用的知识条款
    "model_output": {...} | null            # 当时模型的结构化输出（ReplyDraftOutput 字段）；没有就用人工回复代替
    "model_meta": {"prompt_version", "model", "model_config_version", "rule_version", "knowledge_versions",
                   "compliance_rules_version", "drafted_at"} | 缺省   # 当时起草用的版本（只有 agent 来源有）
  },
  "human_reply": "...",                     # 人工最终发出的回复（已脱敏）
  "expect": {
    "intent": ["shipping_status"], "intent_not": [],
    "action": "answer" | "clarify" | "handoff" | null,
    "handoff": true | false | null,         # 是否该转人工（null = 不评）
    "compliance": "PASS" | "REVIEW" | "BLOCK" | ["PASS", "REVIEW"] | null,   # 列表 = 其中任一都算对
    "must_include": ["1Z999..."], "must_not_include": ["refund guaranteed"]
  },
  "label_source": "auto" | "human" | "synthetic", "needs_review": true | false,
  "meta": {"edit_ratio": 0.12, "intent_confidence": "high", ...}
}
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..intents import INTENTS

SOURCES = ("agent", "manual", "baseline", "synthetic")
ACTIONS = ("answer", "clarify", "handoff")
COMPLIANCE = ("PASS", "REVIEW", "BLOCK")
LABELS = ("auto", "human", "synthetic")
ROOT = Path(__file__).resolve().parents[3]
GOLDEN_FILE = ROOT / "data" / "eval" / "golden.jsonl"
SAMPLE_FILE = ROOT / "tests" / "fixtures" / "golden_sample.jsonl"


def case_id(source: str, raw_id: str) -> str:
    return "G-" + hashlib.sha1(f"{source}:{raw_id}".encode()).hexdigest()[:10]


def validate(case: dict) -> list[str]:
    """格式问题列表（空 = 合格）。"""
    errs: list[str] = []
    for k in ("case_id", "source", "input", "expect"):
        if k not in case:
            errs.append(f"缺 {k}")
    if errs:
        return errs
    if case["source"] not in SOURCES:
        errs.append(f"source 不对：{case['source']}")
    inp, exp = case["input"], case["expect"]
    if not inp.get("inbound") or not all((m.get("text") or "").strip() for m in inp["inbound"]):
        errs.append("没有本次买家消息")
    for m in inp.get("history") or []:
        if m.get("role") not in ("BUYER", "SELLER"):
            errs.append(f"history 角色不对：{m.get('role')}")
    bad = [i for i in exp.get("intent") or [] if i not in INTENTS] + [i for i in exp.get("intent_not") or [] if i not in INTENTS]
    if bad:
        errs.append("意图不在清单里：" + "、".join(bad))
    if exp.get("action") not in (None, *ACTIONS):
        errs.append(f"action 不对：{exp.get('action')}")
    comp = exp.get("compliance")
    if not (comp is None or comp in COMPLIANCE or (isinstance(comp, list) and comp and all(x in COMPLIANCE for x in comp))):
        errs.append(f"compliance 不对：{comp}")
    if case.get("label_source") not in (None, *LABELS):
        errs.append(f"label_source 不对：{case.get('label_source')}")
    if not inp.get("model_output") and not (case.get("human_reply") or "").strip():
        errs.append("既没有模型输出也没有人工回复，回放时没有回复可评")
    return errs


def load(path: Path | str | None = None) -> list[dict]:
    p = Path(path) if path else GOLDEN_FILE  # 调用时再取，测试里换掉 GOLDEN_FILE 才生效
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def dump(cases: list[dict], path: Path | str | None = None) -> Path:
    p = Path(path) if path else GOLDEN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases), encoding="utf-8")
    return p
