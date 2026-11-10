"""多轮对话意图评测：证明“结合上下文理解意图”是否更准，给上线效果页和汇报提供数据。

用例在 tests/eval/intent_cases.json（合成 / 改写后脱敏的多轮对话 + 业务上下文 + 期望意图）。
按用例重建会话，调用真实的回复流水线（AgentRunner），统计：
  - 主意图准确率（模型主意图落在 expect.intent 列表里；不能是 intent_not 里的）
  - 全部意图命中率（intents_required 里的意图被识别出的比例）
  - 沿用判断准确率（expect.inherits_issue 与模型是否填了 inherits_issue 一致。inherits_issue 是会话状态卡里被沿用的
    未解决问题 id，卡片里没有问题时只能是 null，所以只在“有 prior_issue / 在跟的查件单 / 未结 eBay case”的用例上评；
    草稿输出没有这个字段时整项跳过）
  - action 符合率（reply / clarify / handoff 对应模型的 answer / clarify / handoff）
按难点类型（tags）分组，列出判错的用例和模型给的理由（reasoning_zh、intent_basis）。

默认不调用模型：
    python scripts/eval_intents.py                    # 等同 --dry-run：只校验用例格式、估算调用次数和 token
    python scripts/eval_intents.py --live --cases C01,C02,C03   # 真实调用模型，只跑指定用例
    python scripts/eval_intents.py --live              # 全部用例（会先打印预计调用次数和 token，输入 yes 才开始）
    python scripts/eval_intents.py --live --yes        # 不再确认（无人值守时用）
可选：--knowledge-store <店铺ID> 把该店铺当前生效的“可以对买家说”知识复制进评测库（全量注入，token 会多很多）。

回放在独立的 SQLite 评测库里进行（data/eval_intents/<时间>/eval.db），不碰正式库、不发 eBay、不调谷仓 / 17TRACK、
不翻译买家来信、不发飞书；业务工具读本次生成的演示数据（订单、包裹、eBay case），查件单直接写进评测库。
报告：data/eval_intents/<时间>/report.md、report.json。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CASES_FILE = ROOT / "tests" / "eval" / "intent_cases.json"
EVAL_STORE = "EVAL_STORE"
TAG_LABELS = {
    "short_followup": "短追问（有进展吗 / 然后呢 / 还是不行）",
    "after_our_reply": "我们回复后的追问",
    "topic_switch": "中途换话题",
    "multi_issue": "一条消息两件事",
    "thanks_closing": "致谢、结束语",
    "single_turn": "单轮基线",
    "with_ticket": "有在跟的查件单",
    "with_open_case": "有未结 eBay case",
}
ACTION_MAP = {"reply": "answer", "clarify": "clarify", "handoff": "handoff"}
NEW_FIELDS = ("intent_confidence", "intent_basis", "inherits_issue")  # 2 号新增的字段：存在就统计，不存在跳过
CALLS_PER_CASE = 3  # 估算：起草一般 1–3 次模型调用（先调工具再写）。2026-11-09 冒烟实测每组约 1.4 万 token
_AGO = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


# ---------------------------------------------------------------- 用例与校验（纯函数，单测覆盖）
def parse_ago(s: str) -> timedelta:
    m = _AGO.fullmatch((s or "").strip())
    if not m or not any(m.groups()):
        raise ValueError(f"时间格式不对：{s!r}（用 3d、2d20h、30m 这样的写法）")
    d, h, mi = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def split_messages(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """最后一条店铺消息之后的买家消息是本次来信，前面的是历史。"""
    last_seller = max((i for i, m in enumerate(messages) if m["role"] == "SELLER"), default=-1)
    return messages[:last_seller + 1], messages[last_seller + 1:]


def validate(data: dict, intents: set[str]) -> list[str]:
    errs: list[str] = []
    listings = data.get("listings") or {}
    seen = set()
    for c in data.get("cases") or []:
        cid = c.get("case_id") or "?"
        if cid in seen:
            errs.append(f"{cid}：编号重复")
        seen.add(cid)
        if c.get("listing") not in listings:
            errs.append(f"{cid}：listing {c.get('listing')!r} 不在 listings 里")
        for t in c.get("tags") or []:
            if t not in TAG_LABELS:
                errs.append(f"{cid}：未知难点类型 {t}")
        msgs = c.get("messages") or []
        if not msgs:
            errs.append(f"{cid}：没有消息")
        prev = None
        for m in msgs:
            if m.get("role") not in ("BUYER", "SELLER"):
                errs.append(f"{cid}：角色只能是 BUYER / SELLER")
            try:
                ago = parse_ago(m.get("ago", ""))
                if prev is not None and ago > prev:
                    errs.append(f"{cid}：消息时间要从早到晚（{m.get('ago')}）")
                prev = ago
            except ValueError as e:
                errs.append(f"{cid}：{e}")
        if msgs and not split_messages(msgs)[1]:
            errs.append(f"{cid}：最后一条店铺消息之后没有买家来信")
        e = c.get("expect") or {}
        if not e.get("intent"):
            errs.append(f"{cid}：expect.intent 不能为空")
        for k in ("intent", "intents_required", "intent_not"):
            for i in e.get(k) or []:
                if i not in intents:
                    errs.append(f"{cid}：expect.{k} 里的 {i} 不在意图清单（backend/app/intents.py）")
        if e.get("action") not in (None, *ACTION_MAP):
            errs.append(f"{cid}：expect.action 只能是 reply / clarify / handoff")
        if e.get("inherits_issue") not in (None, True, False):
            errs.append(f"{cid}：expect.inherits_issue 只能是 true / false / null")
        pi = c.get("prior_issue")
        if pi is not None and (pi.get("intent") not in intents or not pi.get("summary_zh")):
            errs.append(f"{cid}：prior_issue 要有 intent（意图清单里的）和 summary_zh")
    return errs


def card_has_issue(case: dict) -> bool:
    """回放时会话状态卡里有没有可沿用的问题：前面提过的问题（prior_issue）、在跟的查件单、未结 eBay case。"""
    ctx = case.get("context") or {}
    return bool(case.get("prior_issue") or ctx.get("ticket") or ctx.get("case"))


def score(case: dict, got: dict) -> dict:
    """单个用例打分。got：{intent, intents, action, inherits_supported, inherits_issue, ...}；没有草稿时 intent 为 None。
    inherits_issue 是被沿用的问题 id（或 None）：有 id = 沿用。"""
    e = case["expect"]
    intent, intents = got.get("intent"), set(got.get("intents") or [])
    if intent:
        intents.add(intent)
    required = set(e.get("intents_required") or [])
    out = {
        "primary_ok": bool(intent) and intent in e["intent"] and intent not in (e.get("intent_not") or []),
        "required_total": len(required), "required_hit": len(required & intents),
        "action_ok": None, "inherits_ok": None,
    }
    if e.get("action"):
        out["action_ok"] = got.get("action") == ACTION_MAP[e["action"]]
    if e.get("inherits_issue") is not None and got.get("inherits_supported") and card_has_issue(case):
        out["inherits_ok"] = (got.get("inherits_issue") is not None) == bool(e["inherits_issue"])
    return out


def aggregate(rows: list[dict]) -> dict:
    """rows：[{case_id, tags, score: score() 的结果}] → 总体与按难点类型的统计。比例为 None 表示没有可评的用例。"""
    def summarize(items: list[dict]) -> dict:
        sc = [r["score"] for r in items]
        req_total = sum(s["required_total"] for s in sc)
        act = [s["action_ok"] for s in sc if s["action_ok"] is not None]
        inh = [s["inherits_ok"] for s in sc if s["inherits_ok"] is not None]

        def ratio(n, d):
            return round(n / d, 3) if d else None

        return {"cases": len(sc), "primary_acc": ratio(sum(s["primary_ok"] for s in sc), len(sc)),
                "required_hit_rate": ratio(sum(s["required_hit"] for s in sc), req_total),
                "action_acc": ratio(sum(act), len(act)), "action_n": len(act),
                "inherits_acc": ratio(sum(inh), len(inh)), "inherits_n": len(inh)}

    by_tag: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for t in r["tags"]:
            by_tag[t].append(r)
    return {"overall": summarize(rows), "by_tag": {t: summarize(v) for t, v in sorted(by_tag.items())}}


def failures(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r["score"]["primary_ok"] or r["score"]["required_hit"] < r["score"]["required_total"]
            or r["score"]["action_ok"] is False or r["score"]["inherits_ok"] is False]


def estimate(data: dict) -> dict:
    """不调模型：估算每个用例送给模型的 token（系统提示词 + 意图清单 + 对话 + 业务上下文），乘以每例调用次数。"""
    from app.intents import intent_catalog_text
    from app.knowledge.retriever import estimate_tokens

    prompt = (ROOT / "backend" / "app" / "prompts" / "reply.md").read_text(encoding="utf-8")
    base = estimate_tokens(prompt) + estimate_tokens(intent_catalog_text()) + 1500  # 1500：工具结果、刊登、格式说明
    per = []
    for c in data["cases"]:
        convo = "\n".join(m["text"] for m in c["messages"])
        per.append(base + estimate_tokens(convo) + estimate_tokens(json.dumps(c.get("context") or {}, ensure_ascii=False)))
    calls = len(per) * CALLS_PER_CASE
    return {"cases": len(per), "calls": calls, "input_tokens": sum(per) * CALLS_PER_CASE,
            "output_tokens": len(per) * CALLS_PER_CASE * 600, "per_case_input": round(sum(per) / max(1, len(per)))}


# ---------------------------------------------------------------- 回放（调用真实流水线）
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_fixture(data: dict, cases: list[dict], now: datetime) -> dict:
    """本次评测的演示数据：每个用例一个刊登（同一商品不同站点）、可选订单 / 包裹 / eBay case。"""
    fx = {"_note": "多轮意图评测自动生成（虚构数据）", "updated_at": _iso(now), "stores": [], "buyers": [], "listings": [],
          "products": [], "orders": [], "packages": [], "inventory": [], "cases": []}
    for n, c in enumerate(cases, 1):
        item, buyer, order_id = f"99{n:010d}", f"eval_buyer_{c['case_id'].lower()}", f"99-{n:05d}-00001"
        lst = data["listings"][c["listing"]]
        c["_item_id"], c["_buyer"], c["_order_id"] = item, buyer, order_id
        fx["listings"].append({"item_id": item, "store_id": EVAL_STORE, "marketplace_id": c["marketplace"],
                               "title": lst["title"], "variations": [{"sku": lst["sku"], "aspects": {}}],
                               "item_specifics": {"Price": f"{lst['price']} {lst['currency']}"},
                               "compatibility": lst.get("compatibility") or [], "handling_time": "1 business day",
                               "shipping_options": ["Standard"], "listing_status": "ACTIVE"})
        fx["inventory"].append({"store_id": EVAL_STORE, "sku": lst["sku"], "sellable_quantity": 5, "reserved_quantity": 0,
                                "in_transit_quantity": 0, "warehouse": "EVAL"})
        ctx = c.get("context") or {}
        if ctx.get("order"):
            o = ctx["order"]
            fx["orders"].append({"order_id": order_id, "store_id": EVAL_STORE, "buyer_key": buyer, "item_id": item,
                                 "sku": lst["sku"], "quantity": 1, "marketplace_id": c["marketplace"],
                                 "order_status": o.get("status", "SHIPPED"), "paid_at": _iso(now - parse_ago(o.get("paid", "3d")))})
        if ctx.get("package"):
            p = ctx["package"]
            fx["packages"].append({"order_id": order_id, "package_id": f"PKG-{n:03d}", "carrier": p.get("carrier"),
                                   "tracking_number": f"EVTRK{n:010d}", "tracking_status": p.get("tracking_status"),
                                   "latest_event": p.get("latest_event"), "event_time": _iso(now - parse_ago(p.get("event", "1d"))),
                                   "estimated_delivery": None})
        if ctx.get("case"):
            k = ctx["case"]
            fx["cases"].append({"store_id": EVAL_STORE, "order_id": order_id, "case_type": k.get("case_type"),
                                "case_id": f"EV-{k.get('case_type')}-{n:03d}", "status": k.get("status", "OPEN"),
                                "opened_at": _iso(now - timedelta(days=2)), "due_by": _iso(now + timedelta(days=2)),
                                "last_action": k.get("last_action")})
    return fx


# 评测时一律关掉的开关和外部连接。用 config.set_overrides 设（不是改设置对象）：bootstrap() 里 runtime_settings.load()
# 会清掉 get_settings 缓存，直接改对象的值会全部回到 .env（2026-11-09 第一次完整评测因此真的发了 2 张飞书工单、注册了 17TRACK）
EVAL_OFF = {"redis_url": None, "ebay_sync_enabled": False, "ebay_push_enabled": False, "ebay_channel": "mock",
            "ebay_client_id": None, "ebay_client_secret": None, "ebay_refresh_token": None,
            "feishu_app_id": None, "feishu_app_secret": None, "knowledge_seed_file": None, "demo_seed": False, "business_source": "fixture",
            "knowledge_mode": "full_context", "auto_translate_buyer_messages": False, "auto_send_enabled": False,
            "review_ack_enabled": False, "track17_enabled": False, "track17_api_key": None, "goodcang_app_token": None,
            "goodcang_app_key": None, "feishu_case_enabled": False, "feishu_case_auto_send": False,
            "logistics_ticket_auto_draft": False, "logistics_ticket_auto_submit": False, "logistics_ticket_auto_send": False,
            "logistics_ticket_submit_notice": False, "invoice_auto_generate": False, "invoice_auto_send": False,
            "ship_notice_enabled": False, "mail_enabled": False, "aftersales_mail_enabled": False}


def _check_isolated() -> None:
    """设置完再核对一遍：有任何外部连接或开关没关、库不是评测库，就停下，不往下跑。"""
    from app.config import get_settings

    s = get_settings()
    bad = [k for k, v in EVAL_OFF.items() if hasattr(s, k) and k not in ("knowledge_mode", "business_source", "ebay_channel")
           and getattr(s, k) not in (None, False, "")]
    if s.ebay_channel != "mock":
        bad.append("ebay_channel")
    if "eval.db" not in (s.effective_database_url or ""):
        bad.append("database_url")
    if bad:
        raise RuntimeError("评测环境没有隔离干净，已停止：" + "、".join(bad))


def setup_eval_env(out_dir: Path, fixture: dict, knowledge_store: str | None):
    """切到独立评测库，关掉一切外部副作用，业务工具改读本次生成的演示数据。返回恢复函数（测试里同进程跑完要还原）。"""
    from app import config
    from app import db as dbmod
    from app import sources
    from app.models import KnowledgeChunk, Store
    from app.sources.fixture import FixtureSource

    knowledge = []
    if knowledge_store:  # 先从正式库只读取出知识，再切库
        from sqlalchemy import select

        with dbmod.session_scope() as db:
            knowledge = [{k: getattr(c, k) for k in c.__table__.columns.keys()}
                         for c in db.scalars(select(KnowledgeChunk).where(
                             KnowledgeChunk.store_id == knowledge_store, KnowledgeChunk.review_state == "PUBLISHED",
                             KnowledgeChunk.audience == "PUBLIC_REPLY"))]
    out_dir.mkdir(parents=True, exist_ok=True)
    fx_path = out_dir / "fixture.json"
    fx_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=1), encoding="utf-8")
    fields = set(config.Settings.model_fields)
    values = {k: v for k, v in {**EVAL_OFF, "database_url": f"sqlite:///{(out_dir / 'eval.db').as_posix()}",
                                "database_password": None}.items() if k in fields}
    prev_overrides, prev_source = dict(config._overrides), sources.get_business_source
    config.set_overrides({**prev_overrides, **values})
    src = FixtureSource(fx_path)
    sources.get_business_source = lambda: src  # get_source_for_store 按模块里的名字取，替换后工具读本次数据
    dbmod.reset_engine()
    from app.seed import bootstrap

    bootstrap()
    config.set_overrides({**config._overrides, **values})  # bootstrap 会用评测库的页面配置重设覆盖值：再盖一次
    _check_isolated()

    def restore() -> None:  # 还原配置覆盖值和业务数据源（数据库连接留在评测库，下次 reset_engine 时按配置重连）
        config.set_overrides(prev_overrides)
        sources.get_business_source = prev_source

    with dbmod.session_scope() as db:
        db.add(Store(id=EVAL_STORE, name="评测店铺", timezone="Europe/Berlin", default_marketplace_id="EBAY_DE"))
        for row in knowledge:
            row = {**row, "store_id": EVAL_STORE, "chunk_id": f"EV-{row['chunk_id']}"[:64]}
            db.add(KnowledgeChunk(**row))
    return restore


def queue_case(case: dict, now: datetime) -> int:
    from app import db as dbmod
    from app.ingestion.service import _append_message, get_or_create_conversation, ingest_buyer_message
    from app.models import LogisticsTicket

    history, inbound = split_messages(case["messages"])
    with dbmod.session_scope() as db:
        conv = get_or_create_conversation(db, EVAL_STORE, f"EVAL-{case['case_id']}", case["_buyer"], case["_item_id"],
                                          case["marketplace"], "listing")
        for i, m in enumerate(history):
            when = (now - parse_ago(m["ago"])).replace(tzinfo=None)
            _append_message(db, conv, f"H-{case['case_id']}-{i}", m["role"], "OUT" if m["role"] == "SELLER" else "IN", m["text"], when)
        pi = case.get("prior_issue")
        if pi:  # 模拟线上：前几轮起草后，模型写进状态卡的未解决问题
            from app import conversation_state

            card = conversation_state._empty()
            first = next((m for m in history if m["role"] == "BUYER"), None)
            card["issues"] = [{"id": "m1", "source": "model", "intent": pi["intent"], "summary_zh": pi["summary_zh"],
                               "order_id": case["_order_id"] if (case.get("context") or {}).get("order") else None,
                               "since": (now - parse_ago(first["ago"])).strftime("%m-%d") if first else None,
                               "status": "处理中", "refs": [], "flags": []}]
            conversation_state.save(db, conv, card)
        tk = (case.get("context") or {}).get("ticket")
        if tk:
            db.add(LogisticsTicket(io_code=f"EVCJ{case['case_id']}", reference_no=case["_order_id"], tracking_no="EVTRK",
                                   type_name=tk.get("type"), io_status=3, io_status_text=tk.get("status", "处理中"),
                                   store_id=EVAL_STORE, buyer_key=case["_buyer"], conversation_id=conv.id,
                                   added_at=(now - parse_ago(tk.get("submitted", "1d"))).strftime("%Y-%m-%d %H:%M:%S")))
        job_id = None
        for i, m in enumerate(inbound):
            when = (now - parse_ago(m["ago"])).replace(tzinfo=None)
            job_id = ingest_buyer_message(db, conv=conv, ebay_message_id=f"B-{case['case_id']}-{i}", body=m["text"],
                                          sent_at=when, skip_merge_window=True, actor="eval_intents").job.id
        return job_id


def collect(job_id: int) -> dict:
    from sqlalchemy import select

    from app import db as dbmod
    from app.models import Draft, ReplyJob

    with dbmod.session_scope() as db:
        job = db.get(ReplyJob, job_id)
        d = db.scalar(select(Draft).where(Draft.job_id == job_id).order_by(Draft.id.desc()).limit(1))
        got = {"job_state": job.state, "reasons": job.decision_reasons or [], "last_error": job.last_error,
               "intent": d.intent if d else None, "intents": (d.intents or []) if d else [], "action": d.action if d else None,
               "reasoning_zh": d.reasoning_zh if d else None, "reply": d.text if d else None,
               "tokens": (d.token_usage or {}).get("total_tokens") if d else None}
        for f in NEW_FIELDS:  # 字段名以 2 号最终实现为准：草稿上有就取，没有就是 None（不计入统计）
            got[f] = getattr(d, f, None) if d else None
        got["inherits_supported"] = inherits_supported()
        return got


def inherits_supported() -> bool:
    """起草输出里有没有 inherits_issue 字段（2 号的“意图把握度和依据”上线后才有）。"""
    from app.agent.schemas import ReplyDraftOutput
    from app.models import Draft

    return "inherits_issue" in ReplyDraftOutput.model_fields and hasattr(Draft, "inherits_issue")


def run_live(data: dict, cases: list[dict], out_dir: Path, knowledge_store: str | None) -> list[dict]:
    from app.agent.runner import AgentRunner
    from app.worker import Worker

    now = datetime.now(timezone.utc)
    restore = setup_eval_env(out_dir, build_fixture(data, cases, now), knowledge_store)
    try:
        return _run_cases(cases, now)
    finally:
        restore()


def _run_cases(cases: list[dict], now: datetime) -> list[dict]:
    from app.agent.runner import AgentRunner
    from app.worker import Worker

    jobs = {c["case_id"]: queue_case(c, now) for c in cases}
    worker, runner = Worker(), AgentRunner()
    done = 0
    while (job_id := worker.claim_next_job()) is not None:
        try:
            runner.run(job_id)
        except Exception as e:  # noqa: BLE001  单个失败照样出报告
            worker._mark_failed(job_id, e)
        done += 1
        print(f"  完成 {done}/{len(cases)}", flush=True)
    rows = []
    for c in cases:
        got = collect(jobs[c["case_id"]])
        rows.append({"case_id": c["case_id"], "tags": c["tags"], "language": c["language"], "expect": c["expect"],
                     "inbound": "\n".join(m["text"] for m in split_messages(c["messages"])[1]), "got": got,
                     "score": score(c, got)})
    return rows


# ---------------------------------------------------------------- 报告
def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def write_report(rows: list[dict], meta: dict, out_dir: Path) -> Path:
    agg = aggregate(rows)
    fails = failures(rows)
    seen_new = {f: any(r["got"].get(f) is not None for r in rows) for f in NEW_FIELDS}
    o = agg["overall"]
    lines = [f"# 多轮对话意图评测 {meta['run_id']}", "",
             f"- 用例：{o['cases']} 组（{meta['cases_file']} 版本 {meta['version']}）；模型：{meta['model']}；知识：{meta['knowledge']}",
             f"- 用时 {meta['seconds']:.0f} 秒；草稿 token 合计 {meta['tokens']}",
             f"- 新字段：" + "、".join(f"{f}{'（有）' if v else '（没有，跳过）'}" for f, v in seen_new.items()), "",
             "## 总体", "", "| 指标 | 结果 |", "|---|---|",
             f"| 主意图准确率 | {_pct(o['primary_acc'])} |", f"| 全部意图命中率 | {_pct(o['required_hit_rate'])} |",
             f"| 沿用判断准确率 | {_pct(o['inherits_acc'])}（可评 {o['inherits_n']} 组） |",
             f"| action 符合率 | {_pct(o['action_acc'])}（可评 {o['action_n']} 组） |", "",
             "## 按难点类型", "", "| 类型 | 用例 | 主意图 | 全部意图 | 沿用判断 | action |", "|---|---|---|---|---|---|"]
    for t, v in agg["by_tag"].items():
        lines.append(f"| {TAG_LABELS.get(t, t)} | {v['cases']} | {_pct(v['primary_acc'])} | {_pct(v['required_hit_rate'])} | "
                     f"{_pct(v['inherits_acc'])} | {_pct(v['action_acc'])} |")
    conf = defaultdict(list)
    for r in rows:
        conf[r["got"].get("intent_confidence") or "（没有）"].append(r["score"]["primary_ok"])
    if any(k != "（没有）" for k in conf):
        lines += ["", "## 按意图把握度（intent_confidence）", "", "| 把握度 | 用例 | 主意图准确率 |", "|---|---|---|"]
        for k in ("high", "medium", "low", "（没有）"):
            if conf.get(k):
                lines.append(f"| {k} | {len(conf[k])} | {_pct(sum(conf[k]) / len(conf[k]))} |")
    lines += ["", f"## 判错的用例（{len(fails)}）", ""]
    for r in fails:
        g, e, s = r["got"], r["expect"], r["score"]
        lines += [f"### {r['case_id']}（{'、'.join(TAG_LABELS.get(t, t) for t in r['tags'])}，{r['language']}）",
                  f"- 买家本次来信：{r['inbound']}",
                  f"- 期望主意图 {'/'.join(e['intent'])}；模型 {g['intent'] or '（没有草稿）'}，全部 {', '.join(g['intents']) or '—'}"
                  + ("" if s["primary_ok"] else " ❌"),
                  f"- 必须识别 {', '.join(e.get('intents_required') or []) or '—'}：命中 {s['required_hit']}/{s['required_total']}",
                  f"- 沿用：期望 {e.get('inherits_issue')}，模型 {g.get('inherits_issue') or 'null'}"
                  + (" ❌" if s["inherits_ok"] is False else ""),
                  f"- action：期望 {e.get('action') or '—'}，模型 {g['action'] or '—'}" + (" ❌" if s["action_ok"] is False else ""),
                  f"- 模型理由：{g.get('intent_basis') or g.get('reasoning_zh') or '—'}",
                  f"- 任务状态 {g['job_state']}；原因 {', '.join(g['reasons']) or '—'}" + (f"；错误 {g['last_error']}" if g["last_error"] else ""), ""]
    md = out_dir / "report.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps({"meta": meta, "summary": agg, "rows": rows}, ensure_ascii=False, indent=1,
                                                    default=str), encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings
    from app.intents import INTENTS

    ap = argparse.ArgumentParser(description="多轮对话意图评测（默认只校验、不调模型）")
    ap.add_argument("--cases-file", default=str(CASES_FILE))
    ap.add_argument("--live", action="store_true", help="真实调用模型（花 token）")
    ap.add_argument("--dry-run", action="store_true", help="只校验用例格式、估算调用次数和 token（默认）")
    ap.add_argument("--cases", help="只跑这些用例，逗号分隔，如 C01,C02")
    ap.add_argument("--knowledge-store", help="把该店铺当前生效的对外知识复制进评测库（全量注入）")
    ap.add_argument("--yes", action="store_true", help="真实运行前不再确认")
    args = ap.parse_args(argv)

    data = json.loads(Path(args.cases_file).read_text(encoding="utf-8"))
    errs = validate(data, set(INTENTS))
    if errs:
        print("用例有错误：\n  " + "\n  ".join(errs))
        return 1
    cases = data["cases"]
    if args.cases:
        want = {x.strip() for x in args.cases.split(",") if x.strip()}
        cases = [c for c in cases if c["case_id"] in want]
        if not cases:
            print("没有匹配的用例")
            return 1
    tags = Counter(t for c in cases for t in c["tags"])
    langs = Counter(c["language"] for c in cases)
    est = estimate({**data, "cases": cases})
    print(f"用例 {len(cases)} 组；难点：" + "、".join(f"{TAG_LABELS[t]} {n}" for t, n in tags.most_common()))
    print("语言：" + "、".join(f"{k} {v}" for k, v in langs.most_common()))
    print(f"预计调用模型约 {est['calls']} 次，输入约 {est['input_tokens']:,} token、输出约 {est['output_tokens']:,} token"
          f"（每组约 {est['per_case_input']:,} 输入 token × {CALLS_PER_CASE} 次；不含 --knowledge-store 的知识）")
    if not args.live:
        print("格式校验通过。这是 dry-run，没有调用模型；真实运行加 --live。")
        return 0
    s = get_settings()
    if not s.llm_enabled:
        print("LLM_ENABLED=false，无法真实运行")
        return 1
    if not args.yes and input(f"将调用模型 {s.llm_chat_model} 约 {est['calls']} 次，确认开始请输入 yes：").strip().lower() != "yes":
        print("已取消")
        return 1
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "data" / "eval_intents" / run_id
    started = time.time()
    rows = run_live(data, cases, out_dir, args.knowledge_store)
    meta = {"run_id": run_id, "cases_file": Path(args.cases_file).name, "version": data.get("version"), "model": s.llm_chat_model,
            "knowledge": args.knowledge_store or "不注入", "seconds": time.time() - started,
            "tokens": sum(r["got"]["tokens"] or 0 for r in rows)}
    md = write_report(rows, meta, out_dir)
    o = aggregate(rows)["overall"]
    print(f"主意图 {_pct(o['primary_acc'])}，全部意图 {_pct(o['required_hit_rate'])}，沿用 {_pct(o['inherits_acc'])}，"
          f"action {_pct(o['action_acc'])}；报告：{md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
