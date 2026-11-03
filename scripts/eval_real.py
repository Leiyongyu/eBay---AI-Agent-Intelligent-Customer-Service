"""真实会话回放测试：用店铺最近几天的真实聊天，对比 AI 草稿与当时真人客服的实际回复。

做法：
  1. 从正式库读取已同步的真实 eBay 买家会话（只读）；找出“买家来信 → 店铺真人回复”的每一处，
     连续的多条买家来信合并为一个任务（与合并窗口一致）。
  2. 切换到独立的评测 SQLite 库，按回复前的聊天历史重建会话，跑固定流程 Agent 生成草稿。
     正式库不会产生任何回复任务，也不会向 eBay 发送任何内容。
  3. 输出中文对比报告：买家原文与中文、真人回复与中文、AI 草稿与回译、AI 判断。

注意：
  - 测试时真实买家消息会发给 .env 配置的模型服务（翻译和起草）；报告里的买家用户名已打码。
  - 知识用该店铺在工作台上传、当前生效的正式知识库（复制进评测库）；还没上传时不注入任何知识。
  - 业务工具仍是演示数据源，查不到真实订单。AI 缺的信息会在报告里统计出来，作为接入真实数据的依据。

用法（PyCharm 直接 Run 即可）：
    python scripts/eval_real.py                # 最近 7 天，最多 40 处
    python scripts/eval_real.py --days 3 --max 10
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.guards.risk import GATE_INFO_REASONS, REASON_LABELS  # noqa: E402
from app.models import (  # noqa: E402
    Conversation,
    Draft,
    KnowledgeChunk,
    KnowledgeImport,
    Message,
    ReplyJob,
    Store,
    ToolCall,
)

RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
OUT_DIR = ROOT / "data" / "eval_real" / RUN_ID


# ---------------------------------------------------------------- 1. 从正式库读取（只读）
def load_cases(days: int, limit: int) -> tuple[list[dict], dict]:
    s = get_settings()
    cutoff = dbmod.utcnow() - timedelta(days=days)
    cases, stats = [], Counter()
    with dbmod.session_scope() as db:
        store = db.get(Store, s.ebay_store_id)
        if store is None:
            sys.exit(f"正式库里没有店铺 {s.ebay_store_id}，请先运行 scripts/ebay_sync.py")
        convs = db.scalars(select(Conversation).where(Conversation.channel_source == "ebay",
                                                      Conversation.conversation_type == "FROM_MEMBERS",
                                                      Conversation.store_id == store.id)).all()
        for conv in convs:
            msgs = db.scalars(select(Message).where(Message.conversation_id == conv.id)
                              .order_by(Message.conversation_seq)).all()
            rows = [{"role": m.sender_role, "body": m.body, "at": m.sent_at, "id": m.ebay_message_id} for m in msgs]
            i = 0
            while i < len(rows):
                if rows[i]["role"] != "BUYER":
                    i += 1
                    continue
                j = i
                while j < len(rows) and rows[j]["role"] == "BUYER":
                    j += 1
                buyer_block = rows[i:j]
                k = j
                while k < len(rows) and rows[k]["role"] == "SELLER":
                    k += 1
                human = rows[j:k]
                if buyer_block[-1]["at"] >= cutoff:
                    if human:
                        cases.append({"conv": conv, "history": rows[:i], "buyer": buyer_block, "human": human})
                    else:
                        stats["unanswered"] += 1
                i = k if k > j else j
        timezone_name = store.timezone
        store_name = store.name
        marketplace = store.default_marketplace_id
        seller_present = bool(store.ebay_user_id)
        # 正式知识库（工作台上传的当前生效条款）一并带进评测库，回放与线上用的是同一份知识
        knowledge = [{col.name: getattr(c, col.name) for col in KnowledgeChunk.__table__.columns}
                     for c in db.scalars(select(KnowledgeChunk).where(
                         KnowledgeChunk.store_id.in_((store.id, "ALL")), KnowledgeChunk.review_state == "PUBLISHED"))]
        active = db.scalar(select(KnowledgeImport).where(KnowledgeImport.store_id == store.id,
                                                         KnowledgeImport.status == "ACTIVE"))
        stats["knowledge_import"] = f"#{active.id} {active.file_name}" if active else "无"
    cases.sort(key=lambda c: c["buyer"][-1]["at"], reverse=True)
    stats["answered_total"] = len(cases)
    cases = cases[:limit]
    # 脱离数据库会话：只保留纯数据
    buyer_alias: dict[str, str] = {}
    out = []
    for n, c in enumerate(cases, 1):
        conv = c["conv"]
        alias = buyer_alias.setdefault(conv.buyer_key, f"买家{len(buyer_alias) + 1}")
        out.append({
            "case_id": f"R{n:02d}", "conversation_id": conv.ebay_conversation_id, "buyer_alias": alias,
            "item_id": conv.item_id, "title": conv.conversation_title, "marketplace": conv.marketplace_id,
            "history": c["history"], "buyer": c["buyer"], "human": c["human"],
        })
    meta = {"store_id": s.ebay_store_id, "store_name": store_name, "timezone": timezone_name,
            "marketplace": marketplace, "seller_known": seller_present, "days": days,
            "knowledge": knowledge, **stats}
    return out, meta


# ---------------------------------------------------------------- 2. 在独立评测库里回放
def switch_to_eval_db(meta: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = get_settings()
    s.database_url = f"sqlite:///{(OUT_DIR / 'eval.db').as_posix()}"
    s.database_password = None
    s.redis_url = None
    s.ebay_sync_enabled = False
    s.knowledge_seed_file = None
    s.demo_seed = False
    dbmod.reset_engine()
    from app.seed import bootstrap

    bootstrap()
    with dbmod.session_scope() as db:
        if db.get(Store, s.ebay_store_id) is None:
            db.add(Store(id=s.ebay_store_id, name=meta["store_name"], timezone=meta["timezone"],
                         default_marketplace_id=meta["marketplace"]))
        for row in meta["knowledge"]:
            db.add(KnowledgeChunk(**row))


def build_and_queue(case: dict, meta: dict) -> int:
    from app.ingestion.service import _append_message, get_or_create_conversation, ingest_buyer_message

    s = get_settings()
    with dbmod.session_scope() as db:
        # 用会话的真实站点（同步时按刊登查得）；仍未知的才按店铺默认站点假设，报告中注明
        known = case["marketplace"] and case["marketplace"] != "UNKNOWN"
        conv = get_or_create_conversation(db, s.ebay_store_id, f"REPLAY-{RUN_ID}-{case['case_id']}",
                                          case["buyer_alias"], case["item_id"],
                                          case["marketplace"] if known else meta["marketplace"],
                                          "listing" if known else "test_assumption")
        conv.conversation_title = case["title"]
        for h in case["history"]:
            if h["role"] in ("BUYER", "SELLER"):
                _append_message(db, conv, f"H-{case['case_id']}-{h['id']}", h["role"],
                                "OUT" if h["role"] == "SELLER" else "IN", h["body"], h["at"])
        job_id = None
        for b in case["buyer"]:
            job_id = ingest_buyer_message(db, conv=conv, ebay_message_id=f"B-{case['case_id']}-{b['id']}",
                                          body=b["body"], sent_at=b["at"], skip_merge_window=True,
                                          actor="eval_real").job.id
        return job_id


def run_jobs(n: int, parallel: int) -> None:
    from app.agent.runner import AgentRunner
    from app.worker import Worker

    worker, runner, done, lock = Worker(), AgentRunner(), [0], threading.Lock()

    def loop():
        while (job_id := worker.claim_next_job()) is not None:
            try:
                runner.run(job_id)
            except Exception as e:
                worker._mark_failed(job_id, e)
            with lock:
                done[0] += 1
                print(f"  完成 {done[0]}/{n}", flush=True)

    threads = [threading.Thread(target=loop) for _ in range(max(1, parallel))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def collect(case: dict, job_id: int, translator) -> dict:
    with dbmod.session_scope() as db:
        job = db.get(ReplyJob, job_id)
        draft = db.scalar(select(Draft).where(Draft.job_id == job_id).order_by(Draft.id.desc()).limit(1))
        tools = [(t.tool_name, t.result_status) for t in db.scalars(select(ToolCall).where(ToolCall.job_id == job_id))]
        reasons = job.decision_reasons or []
        row = {
            "case_id": case["case_id"], "buyer_alias": case["buyer_alias"], "item_id": case["item_id"],
            "marketplace": case["marketplace"],
            "title": case["title"], "history_len": len(case["history"]),
            "buyer_text": "\n".join(b["body"] for b in case["buyer"]),
            "buyer_at": case["buyer"][-1]["at"].isoformat(timespec="minutes"),
            "human_text": "\n".join(h["body"] for h in case["human"]),
            "human_delay_min": round((case["human"][0]["at"] - case["buyer"][-1]["at"]).total_seconds() / 60),
            "job_state": job.state, "risk": job.risk_level, "last_error": job.last_error,
            "problems": [r for r in reasons if r not in GATE_INFO_REASONS],
            "action": draft.action if draft else None, "intent": draft.intent if draft else None,
            "intents": draft.intents if draft else [], "language": draft.language if draft else None,
            "missing_facts": draft.missing_facts if draft else [],
            "compliance": (draft.compliance_result or {}).get("result") if draft else None,
            "ai_text": draft.text if draft else "", "ai_zh": draft.text_zh if draft else "",
            "tokens": (draft.token_usage or {}).get("total_tokens") if draft else None,
            "tools": tools,
        }
    row["buyer_zh"] = _safe_translate(translator, row["buyer_text"])
    row["human_zh"] = _safe_translate(translator, row["human_text"])
    return row


def _safe_translate(translator, text: str) -> str:
    if not text.strip():
        return ""
    try:
        return translator.translate(text, "中文")
    except Exception as e:
        return f"（翻译失败：{type(e).__name__}）"


# ---------------------------------------------------------------- 3. 报告
def write_report(rows: list[dict], meta: dict, seconds: float) -> Path:
    s = get_settings()
    n = len(rows)
    actions = Counter(r["action"] or "无草稿" for r in rows)
    intents = Counter(r["intent"] or "—" for r in rows)
    risks = Counter(r["risk"] or "—" for r in rows)
    problems = Counter(p for r in rows for p in r["problems"])
    missing = Counter(m for r in rows for m in r["missing_facts"])
    tool_status = Counter(f"{t}={st}" for r in rows for t, st in r["tools"])
    tokens = [r["tokens"] for r in rows if r["tokens"]]
    lines = [
        f"# 真实会话回放测试 {RUN_ID}",
        "",
        f"- 店铺：{meta['store_id']}；最近 {meta['days']} 天内有真人回复的买家来信 {meta['answered_total']} 处，"
        f"本次回放 {n} 处；另有 {meta.get('unanswered', 0)} 处买家来信至今没有真人回复（未纳入对比）",
        f"- 模型：`{s.llm_chat_model}`；知识库：{meta['knowledge_import']}（{len(meta['knowledge'])} 条已发布条款）；"
        "业务工具为演示数据源，查不到真实订单",
        f"- 站点：{dict(Counter(r['marketplace'] or 'UNKNOWN' for r in rows))}（来自刊登；UNKNOWN 的按店铺默认 {meta['marketplace']} 假设）",
        f"- 用时 {seconds:.0f} 秒；平均 token {sum(tokens) / max(len(tokens), 1):.0f}/条",
        f"- AI 动作：{dict(actions)}",
        f"- 主要意图：{dict(intents.most_common(10))}",
        f"- 风险等级：{dict(risks)}",
        f"- 进入审核的问题原因：{ {REASON_LABELS.get(k, k): v for k, v in problems.most_common()} }",
        f"- 工具结果：{dict(tool_status.most_common())}",
        "",
        "## AI 缺少的信息（模型自报，按出现次数）",
        "",
        *[f"- {m}（{c}）" for m, c in missing.most_common(30)],
        "",
        "## 逐条对比",
        "",
    ]
    for r in rows:
        lines += [
            f"### {r['case_id']}｜{r['marketplace'] or 'UNKNOWN'}｜{r['buyer_alias']}｜{r['title'] or ''}",
            "",
            f"- 来信时间（UTC）{r['buyer_at']}；之前已有 {r['history_len']} 条聊天；真人 {r['human_delay_min']} 分钟后回复",
            f"- AI：动作 {r['action'] or '无草稿'}，意图 {r['intent']} {r['intents']}，风险 {r['risk']}，合规 {r['compliance']}"
            + (f"，问题：{'、'.join(REASON_LABELS.get(p, p) for p in r['problems'])}" if r["problems"] else ""),
            f"- AI 缺少：{'；'.join(r['missing_facts']) or '无'}",
        ]
        if r["last_error"]:
            lines.append(f"- 运行错误：{r['last_error']}")
        lines += [
            "", "**买家来信**", "", "```text", r["buyer_text"], "```", f"> 中文：{r['buyer_zh']}", "",
            "**真人客服实际回复**", "", "```text", r["human_text"], "```", f"> 中文：{r['human_zh']}", "",
            "**AI 草稿**", "", "```text", r["ai_text"] or "（无草稿）", "```",
            *( [f"> 回译：{r['ai_zh']}"] if r["ai_zh"] and r["ai_zh"] != r["ai_text"] else []), "",
        ]
    path = OUT_DIR / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    with (OUT_DIR / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--parallel", type=int, default=4)
    args = ap.parse_args()

    s = get_settings()
    if s.agent_not_ready_reasons():
        sys.exit("AGENT_NOT_READY：" + "；".join(s.agent_not_ready_reasons()))
    cases, meta = load_cases(args.days, args.max)  # 此时仍连接正式库，只读
    print(f"最近 {args.days} 天有真人回复的买家来信 {meta['answered_total']} 处，本次回放 {len(cases)} 处；"
          f"未回复 {meta.get('unanswered', 0)} 处", flush=True)
    if not cases:
        return 0
    switch_to_eval_db(meta)
    print(f"评测库：{OUT_DIR / 'eval.db'}（正式库不受影响）", flush=True)

    started = time.perf_counter()
    job_ids = {c["case_id"]: build_and_queue(c, meta) for c in cases}
    run_jobs(len(cases), args.parallel)

    from app.providers.chat import get_chat_provider

    translator = get_chat_provider()
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        rows = list(pool.map(lambda c: collect(c, job_ids[c["case_id"]], translator), cases))
    report = write_report(rows, meta, time.perf_counter() - started)
    print(f"完成。报告：{report}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
