"""人工修改回流（第二批 A5，规则、不调模型）：

- 修改类型：比较 AI 草稿和人工最终发出的文本——
  fact 事实（运单号、订单号、金额、日期、数字、承运商有增删改）/ length 长度（变化 ≥ 30%）/
  tone 语气（致歉、问候、致谢、结束语有增删）/ knowledge 漏知识（人工补了保修、退货、天数、发票、关税等政策内容）/
  language 语言（换了语言）/ other 其他（改得多但看不出类型）；
- 每周改进建议：近 7 天修改比例 ≥ EDIT_MIN（或人工整段重写）的按（意图, 修改类型）聚类，生成建议，人工确认后才采纳；
- 知识缺口：知识库没命中（检索无结果 / 分数低 / 草稿没引用知识条款）且被人工大改的会话 → 候选知识条款，
  一键导出成知识库 Excel 行（启用=否，人工改写成通用说法后再启用）。
所有例子都经 PiiMasker 脱敏。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..guards.pii import PiiMasker, known_pii
from ..intents import INTENTS
from . import autolabel

EDIT_MIN = 0.2      # 修改比例达到多少才算“被改了”
HEAVY_EDIT = 0.5    # 知识缺口：大改
LOW_RRF = 0.02      # 检索最高分低于它算“没怎么命中”
EDIT_TYPES = {"fact": "事实", "length": "长度", "tone": "语气", "knowledge": "漏知识", "language": "语言", "other": "其他"}
TARGET = {"fact": "prompt", "length": "prompt", "tone": "prompt", "knowledge": "knowledge", "language": "prompt", "other": "prompt"}

NUM = re.compile(r"\b\d[\d.,:/\-]*\d\b|\b\d\b")
TONE = re.compile(r"\b(?:sorry|apolog\w*|unfortunately|entschuldig\w*|leider|désolé\w*|malheureusement|scus\w*|purtroppo|"
                  r"lo\s+siento|lamenta\w*|thank\w*|danke\w*|merci|grazie|gracias|kind\s+regards|best\s+regards|"
                  r"mit\s+freundlichen|cordialement|cordiali|saludos|have\s+a\s+nice|schönen\s+tag|bonne\s+journée)\b|抱歉|感谢|谢谢",
                  re.I)
POLICY = re.compile(r"\b(?:warrant\w*|garantie|guarantee|garanzia|garantía|return\w*|rücksend\w*|rückgabe|retour|reso|devoluci\w*|"
                    r"refund\w*|erstatt\w*|rembours\w*|rimbors\w*|reembols\w*|\d+\s*(?:days?|tage[n]?|jours?|giorni|días)|"
                    r"invoice|rechnung|facture|fattura|factura|customs|zoll|douane|dogana|aduana|vat|mwst)\b|保修|退货|退款|发票|关税",
                    re.I)


def _facts(t: str) -> set[str]:
    out = {m.group(0) for m in NUM.finditer(t or "")}
    for rx in autolabel.FACT_PATTERNS:
        out |= {m.group(0).lower() for m in rx.finditer(t or "")}
    return out


def edit_types(draft: str, final: str, edit_ratio: float | None = None) -> list[str]:
    """人工把草稿改成了什么样（可以多类）。"""
    d, f = draft or "", final or ""
    types: list[str] = []
    if _facts(d) != _facts(f):
        types.append("fact")
    if d and abs(len(f) - len(d)) / max(len(d), 1) >= 0.3:
        types.append("length")
    if {m.group(0).lower() for m in TONE.finditer(d)} != {m.group(0).lower() for m in TONE.finditer(f)}:
        types.append("tone")
    if {m.group(0).lower() for m in POLICY.finditer(f)} - {m.group(0).lower() for m in POLICY.finditer(d)}:
        types.append("knowledge")
    ld, lf = autolabel.language(d), autolabel.language(f)
    if ld and lf and ld != lf:
        types.append("language")
    if not types and (edit_ratio or 0) >= EDIT_MIN:
        types.append("other")
    return types


def week_start(d: date | None = None) -> date:
    d = d or utcnow().date()
    return d - timedelta(days=d.weekday())


def collect(db: Session, since: datetime, until: datetime | None = None) -> list[dict]:
    """时间段内审核员处理过的 AI 草稿：草稿、最终发出的文本、修改比例、修改类型、知识命中情况（全部脱敏）。"""
    from ..models import (AgentRun, AgentStep, Conversation, Draft, Message, ReplyJob, ReplyJobSource, ReviewFeedback,
                          SendIntent, ToolCall)

    until = until or utcnow()
    rows = []
    q = (select(ReviewFeedback, Draft).join(Draft, Draft.id == ReviewFeedback.draft_id)
         .where(ReviewFeedback.created_at >= since, ReviewFeedback.created_at < until,
                ReviewFeedback.action.in_(("APPROVE", "EDIT_APPROVE", "MANUAL"))))
    for fb, d in db.execute(q):
        agent = d if d.author == "agent" else db.scalar(
            select(Draft).where(Draft.job_id == d.job_id, Draft.author == "agent").order_by(Draft.id.desc()).limit(1))
        if agent is None or not (agent.text or "").strip():
            continue  # 没有 AI 草稿：纯人工撰写，不算修改
        intent = db.scalar(select(SendIntent).where(SendIntent.job_id == fb.job_id))
        final = (intent.text if intent else None) or d.text or ""
        job = db.get(ReplyJob, fb.job_id)
        conv = db.get(Conversation, job.conversation_id)
        ratio = fb.edit_ratio if fb.edit_ratio is not None else (1.0 if fb.action == "MANUAL" else 0.0)
        top = None
        run = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.run_generation == agent.run_generation))
        if run is not None:
            step = db.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node == "load_knowledge"))
            hits = ((step.detail or {}).get("hits") or []) if step else []
            top = max((h.get("rrf") or 0 for h in hits), default=0.0) if hits else (None if (step and (step.detail or {}).get(
                "mode") == "full_context") else 0.0)
        tools = [(t.result_data or {}).get("data") for t in db.scalars(select(ToolCall).where(ToolCall.job_id == job.id))]
        known = known_pii(tools)
        masker = PiiMasker(known={**known, "NAME": [conv.buyer_key, *known["NAME"]]})
        src = set(db.scalars(select(ReplyJobSource.source_message_id).where(ReplyJobSource.job_id == job.id)))
        buyer = [m.body for m in db.scalars(select(Message).where(Message.conversation_id == conv.id,
                                                                  Message.ebay_message_id.in_(src or {"-"})))]
        kb_cited = sum(1 for e in agent.evidence_ids or [] if e.startswith("kb:"))
        rows.append({
            "job_id": job.id, "draft_id": agent.id, "store_id": conv.store_id, "conversation_id": conv.id,
            "marketplace": conv.marketplace_id, "intent": agent.intent or "other", "action": fb.action, "edit_ratio": ratio,
            "types": edit_types(agent.text, final, ratio) if ratio >= EDIT_MIN else [],
            "draft": masker.mask(agent.text), "final": masker.mask(final), "buyer": [masker.mask(b or "") for b in buyer],
            "kb_cited": kb_cited, "no_kb": "NO_KB_EVIDENCE" in (agent.review_reasons or []) or kb_cited == 0,
            "retrieval_top": top, "intent_confidence": agent.intent_confidence, "at": fb.created_at,
        })
    return rows


def edit_stats(rows: list[dict]) -> dict:
    """修改统计：按修改类型、按意图。"""
    edited = [r for r in rows if r["edit_ratio"] >= EDIT_MIN]
    by_type: dict[str, int] = defaultdict(int)
    by_intent: dict[str, dict] = defaultdict(lambda: {"n": 0, "edited": 0, "sum_ratio": 0.0})
    for r in rows:
        b = by_intent[r["intent"]]
        b["n"] += 1
        b["sum_ratio"] += r["edit_ratio"]
        if r["edit_ratio"] >= EDIT_MIN:
            b["edited"] += 1
            for t in r["types"]:
                by_type[t] += 1
    return {"reviewed": len(rows), "edited": len(edited),
            "edited_rate": round(len(edited) / len(rows), 3) if rows else None,
            "by_type": {k: {"label": EDIT_TYPES[k], "n": v} for k, v in sorted(by_type.items(), key=lambda x: -x[1])},
            "by_intent": {k: {"n": v["n"], "edited": v["edited"], "edited_rate": round(v["edited"] / v["n"], 3),
                              "avg_edit_ratio": round(v["sum_ratio"] / v["n"], 3)} for k, v in sorted(by_intent.items())}}


def _suggest(intent: str, typ: str, rs: list[dict]) -> str:
    name = INTENTS.get(intent, (intent,))[0]
    n = len(rs)
    if typ == "fact":
        return (f"「{name}」近一周 {n} 条草稿被人工改了事实（运单号 / 日期 / 金额 / 数字 / 承运商）：核对这类回复用的查询结果和"
                f"提示词里“只按工具结果回答”的规则，看例子里哪些数字是 AI 编的或取错的")
    if typ == "length":
        longer = sum(1 for r in rs if len(r["final"]) > len(r["draft"]))
        return (f"「{name}」近一周 {n} 条被人工明显{'加长' if longer > n / 2 else '删短'}：提示词里对这类回复的篇幅要求"
                f"{'太短，缺必要信息' if longer > n / 2 else '偏长，套话多'}，可以调整示例或加长度要求")
    if typ == "tone":
        return f"「{name}」近一周 {n} 条被人工改了语气（致歉 / 问候 / 致谢 / 结束语）：看例子统一这类回复的开头和结尾，写进提示词或话术模板"
    if typ == "knowledge":
        return (f"「{name}」近一周 {n} 条人工补了政策类内容（保修、退货、天数、发票、关税等），AI 没写：可能缺知识条款或检索没命中，"
                f"到“知识缺口”里看候选条款")
    if typ == "language":
        return f"「{name}」近一周 {n} 条人工换了回复语言：检查语言判断（买家最新一条来信的语言）"
    return f"「{name}」近一周 {n} 条被人工大改但看不出具体类型：抽例子人工看"


def generate_suggestions(db: Session, week: date | None = None) -> list:
    """生成（或刷新）这周的改进建议：上周一到这周一前的修改。已确认 / 不采纳的保留状态，只更新条数和例子。"""
    from ..models import ImprovementSuggestion

    ws = week or week_start()
    since = datetime.combine(ws - timedelta(days=7), datetime.min.time())
    rows = collect(db, since, datetime.combine(ws, datetime.min.time()) if week else None)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        for t in r["types"]:
            groups[(r["intent"], t)].append(r)
    out = []
    now = utcnow()
    for (intent, typ), rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = db.scalar(select(ImprovementSuggestion).where(ImprovementSuggestion.week == ws.isoformat(),
                                                          ImprovementSuggestion.intent == intent,
                                                          ImprovementSuggestion.edit_type == typ))
        if s is None:
            s = ImprovementSuggestion(week=ws.isoformat(), intent=intent[:32], edit_type=typ, target=TARGET[typ], created_at=now)
            db.add(s)
        s.count = len(rs)
        s.avg_edit_ratio = round(sum(r["edit_ratio"] for r in rs) / len(rs), 3)
        s.suggestion = _suggest(intent, typ, rs)
        s.examples = [{"draft": r["draft"][:600], "final": r["final"][:600], "edit_ratio": r["edit_ratio"], "job_id": r["job_id"]}
                      for r in sorted(rs, key=lambda r: -r["edit_ratio"])[:3]]
        s.updated_at = now
        out.append(s)
    db.flush()
    return out


def knowledge_gaps(db: Session, days: int = 30) -> list[dict]:
    """知识缺口：知识库没命中（检索没结果 / 分数低 / 草稿没引用知识条款）且被人工大改 → 候选知识条款。"""
    rows = collect(db, utcnow() - timedelta(days=days))
    out = []
    for r in rows:
        low_hit = r["retrieval_top"] is not None and r["retrieval_top"] < LOW_RRF
        if not (r["no_kb"] or low_hit) or r["edit_ratio"] < HEAVY_EDIT:
            continue
        q = " ".join(r["buyer"]).strip()
        out.append({"job_id": r["job_id"], "conversation_id": r["conversation_id"], "store_id": r["store_id"],
                    "marketplace": r["marketplace"], "intent": r["intent"], "question": q[:800], "answer": r["final"][:2000],
                    "draft": r["draft"][:800], "edit_ratio": r["edit_ratio"], "retrieval_top": r["retrieval_top"],
                    "language": autolabel.language(r["final"]) or "en",
                    "title": f"{INTENTS.get(r['intent'], (r['intent'],))[0]}：{q[:40]}"})
    return out


INTENT_TOPIC = {"shipping_status": "物流", "delivery_delay": "物流", "customs_duty": "发票税务", "invoice_vat": "发票税务",
                "return_refund": "退货", "refund_status": "退款", "damaged_wrong": "售后", "quality_issue": "售后",
                "install_help": "安装", "fitment": "适配", "item_spec": "商品", "stock": "商品", "price_offer": "价格",
                "shipping_cost": "发货", "handling_time": "发货", "cancel_request": "订单", "address_change": "订单",
                "order_how_to": "订单"}


def gaps_workbook(gaps: list[dict], store_names: dict[str, str]):
    """候选知识条款 → 知识库 Excel（表头和知识库导入一致；启用=否，人工改写成通用说法后再启用）。"""
    from openpyxl import Workbook

    from ..knowledge.excel_import import COLUMNS

    wb = Workbook()
    ws = wb.active
    ws.title = "知识条款"
    ws.append([c for c, _, _ in COLUMNS])
    today = utcnow().date().isoformat()
    for g in gaps:
        row = {"编号": "", "标题": g["title"][:60], "正文": g["answer"], "适用站点": g["marketplace"] or "全部",
               "能否对买家说": "可以", "主题": INTENT_TOPIC.get(g["intent"], "其他"), "优先级": 50, "语言": g["language"],
               "生效日期": today, "失效日期": "", "相关SKU": "", "启用": "否",
               "负责人/来源": f"人工修改回流 {today}（任务 {g['job_id']}）"[:64], "适用场景": "",
               "适用店铺": store_names.get(g["store_id"], g["store_id"]), "参考文档": ""}
        ws.append([row.get(c, "") for c, _, _ in COLUMNS])
    return wb


def weekly_due(db: Session, now: datetime | None = None) -> bool:
    """每周一凌晨 3 点后、这周还没生成过建议。"""
    from ..models import ImprovementSuggestion

    now = now or datetime.now()
    if now.weekday() != 0 or now.hour < 3:
        return False
    ws = week_start(now.date()).isoformat()
    return db.scalar(select(ImprovementSuggestion.id).where(ImprovementSuggestion.week == ws).limit(1)) is None
