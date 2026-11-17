"""第一层规则评测（全部 mock，不调模型、不连外部系统）：在当前数据库里回放黄金用例，跑当前的整条起草流水线，断言结果。

回放方式：
  - 每条用例一个独立店铺（知识条款互不串）：写入历史消息、本次买家来信（ingest_buyer_message 建回复任务）、当时引用的知识条款；
  - 工具执行换成“当时的结果”（按工具名依次取；没有记录的返回 NOT_FOUND）；
  - 模型换成假模型，原样返回当时的结构化输出；没有当时的模型输出（历史 / 人工配对）时用“替身输出”：
    intent=other、action=answer、needs_human=false、reply_text=人工最终回复——这时意图、action 不评（喂进去的就是答案），
    只评程序自己的判断：是否转人工（买家原文规则 → L3）、合规、必含要点、禁止话；
  - 合成用例没有回复，mock 下跳过（要 --live 才评）。
断言（每项 True / False / None=不评）：intent、action、handoff、compliance、must_include、must_not_include；
一条用例所有要评的项都对才算通过。按主意图（stratum）出通过率。
能挡住的是程序侧的回归：校验、风险闸门、合规规则、转人工规则、提示词拼装、PII 还原等；模型输出的变化要第二层评审或 --live。
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import re
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

AGO = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")
CHECKS = ("intent", "action", "handoff", "compliance", "must_include", "must_not_include")


def parse_ago(s: str | None) -> timedelta:
    m = AGO.fullmatch((s or "0m").strip()) or AGO.fullmatch("0m")
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def standin_output(case: dict) -> dict:
    """没有当时模型输出时的替身：把人工最终回复当成草稿，意图 / action 不给程序任何提示。"""
    return {"intent": "other", "intents": ["other"], "intent_confidence": "high", "intent_basis": [], "inherits_issue": None,
            "language": case.get("language") or "en", "action": "answer", "needs_human": False, "claims": [],
            "missing_facts": [], "conflicts": [], "reasoning_zh": "黄金集替身输出（人工最终回复）",
            "reply_text": case.get("human_reply") or "", "reply_zh": "", "situation_stage": "other", "situation_zh": "",
            "reason_code": "golden_standin"}


def mode_of(case: dict) -> str:
    """recorded 有当时模型输出 / standin 用人工回复替身 / skip mock 下评不了（合成用例没有回复）。"""
    if case["input"].get("model_output"):
        return "recorded"
    if (case.get("human_reply") or "").strip():
        return "standin"
    return "skip"


@contextlib.contextmanager
def recorded_tools(tools: list[dict]):
    """把工具执行换成当时的结果（按工具名依次取）。"""
    from ..tools import registry

    queues: dict[str, list[dict]] = defaultdict(list)
    for t in tools or []:
        queues[t["tool_name"]].append(t)
    saved = dict(registry.TOOLS)

    def make(name):
        def fn(ctx, src, args, **kw):
            q = queues.get(name)
            if not q:
                return "NOT_FOUND", None, None, "回放：当时没有这个工具结果"
            t = q.pop(0) if len(q) > 1 else q[0]
            return t.get("status") or "FOUND", t.get("data"), t.get("evidence_ref"), t.get("message")
        return fn

    try:
        for name, spec in saved.items():
            registry.TOOLS[name] = dataclasses.replace(spec, fn=make(name))
        yield
    finally:
        registry.TOOLS.clear()
        registry.TOOLS.update(saved)


@contextlib.contextmanager
def fake_model(output: dict):
    """假模型：每次调用都原样返回这份结构化输出（修复重试也一样）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from ..config import get_settings
    from ..providers.chat import ChatModelProvider, set_chat_provider_override

    text = json.dumps(output, ensure_ascii=False)
    model = GenericFakeChatModel(messages=iter([AIMessage(content=text) for _ in range(6)]))
    set_chat_provider_override(ChatModelProvider(get_settings(), model=model))
    try:
        yield
    finally:
        set_chat_provider_override(None)


def seed(case: dict, now) -> int | None:
    """写入店铺、消息、知识条款，建回复任务。返回 job_id（来信没建出任务时为 None）。"""
    from ..db import session_scope
    from ..ingestion.service import _append_message, get_or_create_conversation, ingest_buyer_message
    from ..models import KnowledgeChunk, Store

    cid = case["case_id"]
    store_id = f"GS-{cid}"[:64]
    mk = case.get("marketplace") or "EBAY_DE"
    with session_scope() as db:
        if db.get(Store, store_id) is None:
            db.add(Store(id=store_id, name=f"评测 {cid}", timezone="Europe/Berlin", default_marketplace_id=mk))
            db.flush()
        for i, k in enumerate(case["input"].get("knowledge") or []):
            text = k.get("text") or ""
            db.merge(KnowledgeChunk(chunk_id=f"{cid}-K{i}"[:64], clause_id=k["clause_id"], document_id="GOLDEN",
                                    version=str(k.get("version") or "1"), title=k.get("title") or k["clause_id"], text=text,
                                    store_id=store_id, marketplace_id="ALL", audience="PUBLIC_REPLY",
                                    language=case.get("language") or "en", topic="golden", priority=50,
                                    effective_at="2000-01-01", review_state="PUBLISHED",
                                    content_hash=hashlib.sha256(text.encode()).hexdigest()))
        conv = get_or_create_conversation(db, store_id, f"GOLD-{cid}", "golden_buyer", None, mk, "golden")
        conv.conversation_title = case["input"].get("title") or None
        for i, m in enumerate(case["input"].get("history") or []):
            when = (now - parse_ago(m.get("ago"))).replace(tzinfo=None)
            _append_message(db, conv, f"GH-{cid}-{i}", m["role"], "OUT" if m["role"] == "SELLER" else "IN", m["text"], when)
        job_id = None
        for i, m in enumerate(case["input"]["inbound"]):
            when = (now - parse_ago(m.get("ago"))).replace(tzinfo=None)
            r = ingest_buyer_message(db, conv=conv, ebay_message_id=f"GB-{cid}-{i}", body=m["text"], sent_at=when,
                                     skip_merge_window=True, actor="eval_rules")
            job_id = r.job.id if r.job else job_id
        return job_id


def claim(job_id: int) -> bool:
    """像 Worker 一样认领这个任务（RECEIVED → RUNNING），流水线只给认领了的任务落库。"""
    from sqlalchemy import update

    from ..db import session_scope, utcnow
    from ..models import JobState, ReplyJob

    with session_scope() as db:
        job = db.get(ReplyJob, job_id)
        if job is None or job.state not in (JobState.RECEIVED, JobState.RETRYABLE_ERROR):
            return False
        return db.execute(update(ReplyJob).where(ReplyJob.id == job_id, ReplyJob.lock_version == job.lock_version)
                          .values(state=JobState.RUNNING, lock_version=job.lock_version + 1, attempt=job.attempt + 1,
                                  updated_at=utcnow())).rowcount == 1


def collect(job_id: int | None) -> dict:
    from ..db import session_scope
    from ..models import Draft, ReplyJob

    if job_id is None:
        return {"error": "来信没有建出回复任务"}
    with session_scope() as db:
        job = db.get(ReplyJob, job_id)
        d = db.scalar(select(Draft).where(Draft.job_id == job_id, Draft.author == "agent").order_by(Draft.id.desc()).limit(1))
        if d is None:
            return {"error": f"没有生成草稿（任务状态 {job.state}，{(job.last_error or '')[:200]}）",
                    "reasons": job.decision_reasons or []}
        return {"intent": d.intent, "intents": d.intents or [], "action": d.action, "text": d.text or "",
                "risk_level": d.risk_level, "decision": d.decision, "reasons": d.review_reasons or job.decision_reasons or [],
                "compliance": (d.compliance_result or {}).get("result")}


def handoff_of(got: dict) -> bool:
    """转人工：模型要转人工，或程序判到 L3（法律 / 安全 / 站外交易 / 评价等，任何配置下都只能人工处理）。"""
    return got.get("action") == "handoff" or got.get("risk_level") == "L3" or "MODEL_REQUESTED_HUMAN" in (got.get("reasons") or [])


def score(case: dict, got: dict, mode: str) -> dict:
    exp = case["expect"]
    c: dict[str, bool | None] = {k: None for k in CHECKS}
    if "error" in got:
        return {"checks": {k: False for k in CHECKS}, "ok": False, "error": got["error"]}
    text = (got.get("text") or "").lower()
    if mode in ("recorded", "live"):
        if exp.get("intent"):
            c["intent"] = got["intent"] in exp["intent"] and got["intent"] not in (exp.get("intent_not") or [])
        if exp.get("action"):
            c["action"] = got["action"] == exp["action"]
    if exp.get("handoff") is not None:
        c["handoff"] = handoff_of(got) == bool(exp["handoff"])
    if exp.get("compliance"):
        want = exp["compliance"] if isinstance(exp["compliance"], list) else [exp["compliance"]]
        c["compliance"] = got.get("compliance") in want
    if exp.get("must_include"):
        c["must_include"] = all(p.lower() in text for p in exp["must_include"])
    if exp.get("must_not_include"):
        c["must_not_include"] = not any(p.lower() in text for p in exp["must_not_include"])
    judged = [v for v in c.values() if v is not None]
    return {"checks": c, "ok": all(judged) if judged else None}


def run_case(case: dict, now, live: bool = False) -> dict:
    """live=True：不换模型，真实起草（工具仍用当时的结果，不连外部系统），意图 / action 也评。"""
    from ..agent.runner import AgentRunner

    mode = "live" if live else mode_of(case)
    if mode == "skip":
        return {"case_id": case["case_id"], "mode": mode, "stratum": case.get("stratum"), "ok": None,
                "source": case.get("source"), "label_source": case.get("label_source") or "auto",
                "note": "mock 下没有回复可评（合成用例），要 --live"}
    out = case["input"]["model_output"] if mode == "recorded" else standin_output(case)
    with recorded_tools(case["input"].get("tools") or []), (contextlib.nullcontext() if live else fake_model(out)):
        job_id = seed(case, now)
        err = None
        if job_id is not None and not claim(job_id):
            err = "回复任务认领不到（状态不是待处理）"
        elif job_id is not None:
            try:
                AgentRunner().run(job_id)
            except Exception as e:  # noqa: BLE001  单条出错照样出报告
                err = f"{type(e).__name__}: {str(e)[:300]}"
    got = collect(job_id) if not err else {"error": err}
    s = score(case, got, mode)
    return {"case_id": case["case_id"], "mode": mode, "source": case.get("source"), "label_source": case.get("label_source") or "auto",
            "prompt_version": (case["input"].get("model_meta") or {}).get("prompt_version"), "stratum": case.get("stratum"),
            "language": case.get("language"), "got": got, **s}


def _count(rows: list[dict], key: str) -> dict:
    out: dict = defaultdict(int)
    for r in rows:
        out[r.get(key) or "?"] += 1
    return dict(out)


def _group(rows: list[dict], key: str, rate) -> dict:
    g: dict = defaultdict(list)
    for r in rows:
        g[r.get(key) or "?"].append(r)
    return {k: {"n": len(v), "pass_rate": rate(v)} for k, v in sorted(g.items())}


def aggregate(rows: list[dict]) -> dict:
    judged = [r for r in rows if r.get("ok") is not None]

    def rate(rs):
        return round(sum(1 for r in rs if r["ok"]) / len(rs), 3) if rs else None

    by_stratum: dict[str, list] = defaultdict(list)
    for r in judged:
        by_stratum[r["stratum"] or "other"].append(r)
    by_check = {}
    for k in CHECKS:
        vals = [r["checks"][k] for r in judged if r.get("checks", {}).get(k) is not None]
        by_check[k] = {"n": len(vals), "pass_rate": round(sum(vals) / len(vals), 3) if vals else None}
    return {"cases": len(rows), "judged": len(judged), "skipped": len(rows) - len(judged), "pass_rate": rate(judged),
            "by_mode": {m: rate([r for r in judged if r["mode"] == m]) for m in ("recorded", "standin", "live")},
            "by_check": by_check,
            "by_stratum": {k: {"n": len(v), "pass_rate": rate(v)} for k, v in sorted(by_stratum.items())},
            # 来源、标签来源、当时的提示词版本（有当时模型输出的用例）分开看：分数变了能分清是规则改了还是数据换了
            "by_source": _group(judged, "source", rate), "by_label_source": _group(judged, "label_source", rate),
            "by_prompt_version": _group([r for r in judged if r.get("prompt_version")], "prompt_version", rate),
            "dataset": {"source": _count(rows, "source"), "label_source": _count(rows, "label_source")},
            "failures": [{"case_id": r["case_id"], "stratum": r["stratum"], "error": r.get("error"),
                          "failed": [k for k, v in (r.get("checks") or {}).items() if v is False]}
                         for r in judged if not r["ok"]][:50]}


def run(cases: list[dict], live: bool = False) -> dict:
    from ..db import utcnow

    now = utcnow()
    rows = [run_case(c, now, live) for c in cases]
    return {"summary": aggregate(rows), "rows": rows}
