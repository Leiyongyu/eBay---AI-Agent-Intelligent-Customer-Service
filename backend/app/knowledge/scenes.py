"""仅内部条款的“适用场景”：会话属于哪些场景，就在会话页“内部参考”里列出挂了这些场景的仅内部条款。

场景名是固定清单，在知识库 Excel 的「适用场景」列里填（多个用逗号分隔）；“常驻”表示每个会话都显示。
会话的场景按可信度依次取：客服标的售后类型 → 没完结的售后群工单类别 → AI 草稿识别的意图 → 会话阶段。
只查数据库，不生成向量、不调用大模型；内部条款仍然不会进入 AI 的提示词。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AftersalesCase, Conversation, Draft, KnowledgeChunk, ReplyJob

ALWAYS = "常驻"
SCENES = ["适配", "商品参数", "质量问题", "破损错发缺件", "不适配", "退货退款", "物流异常", "取消改地址", "发票", "评价", "议价",
          ALWAYS]
MAX_REFS = 10  # 常驻之外最多显示几条

# 客服标的售后类型（models.Aftersales.TYPES）
AFTERSALES_SCENES = {
    "退货": ["退货退款"], "退款": ["退货退款"], "赔偿/部分退款": ["退货退款"], "不适配": ["不适配"],
    "破损/错发/缺件": ["破损错发缺件"], "质量问题": ["质量问题"], "物流异常/未收到": ["物流异常"],
    "物流问题需判断补发": ["物流异常"], "延迟发货": ["物流异常"], "取消订单": ["取消改地址"], "改地址": ["取消改地址"],
}
# 飞书售后群工单类别（feishu/cases.py）
CASE_SCENES = {"质量问题": ["质量问题"], "破损错发缺件": ["破损错发缺件"], "安装技术问题": ["适配"], "适配核对": ["适配"]}
# AI 识别的意图（intents.py）
INTENT_SCENES = {
    "fitment": ["适配"], "item_spec": ["商品参数"], "damaged_wrong": ["破损错发缺件"], "quality_issue": ["质量问题"],
    "install_help": ["适配"],
    "return_refund": ["退货退款"], "refund_status": ["退货退款"], "delivery_delay": ["物流异常"],
    "cancel_request": ["取消改地址"], "address_change": ["取消改地址"], "invoice_vat": ["发票"],
    "feedback": ["评价"], "price_offer": ["议价"],
}
# 会话阶段（Conversation.situation_stage）：只在上面都没有时兜底
STAGE_SCENES = {"invoice": ["发票"], "shipping": ["物流异常"]}


def parse_scenes(raw: str) -> tuple[list[str], list[str]]:
    """Excel 单元格 → (场景列表, 不认识的名称)。"""
    import re

    names = [s.strip() for s in re.split(r"[,，、;；]", raw or "") if s.strip()]
    ok = list(dict.fromkeys(n for n in names if n in SCENES))
    return ok, [n for n in names if n not in SCENES]


def conversation_scenes(db: Session, conv: Conversation) -> list[dict]:
    """[{scene, why}]，按可信度排序、去重。"""
    out: dict[str, str] = {}

    def add(scenes: list[str], why: str):
        for s in scenes:
            out.setdefault(s, why)

    if conv.aftersales_type:
        add(AFTERSALES_SCENES.get(conv.aftersales_type, []), f"售后类型：{conv.aftersales_type}")
    case = db.scalar(select(AftersalesCase).where(AftersalesCase.conversation_id == conv.id,
                                                  AftersalesCase.status != "CLOSED")
                     .order_by(AftersalesCase.id.desc()).limit(1))
    if case:
        add(CASE_SCENES.get(case.category, []), f"售后工单：{case.category}")
    draft = db.scalar(select(Draft).join(ReplyJob, ReplyJob.id == Draft.job_id)
                      .where(ReplyJob.conversation_id == conv.id, Draft.author == "agent")
                      .order_by(Draft.id.desc()).limit(1))
    if draft:
        for intent in [draft.intent, *(draft.intents or [])]:
            add(INTENT_SCENES.get(intent or "", []), f"AI 识别：{intent}")
    if not out and conv.situation_stage:
        add(STAGE_SCENES.get(conv.situation_stage, []), f"会话阶段：{conv.situation_stage}")
    return [{"scene": s, "why": w} for s, w in out.items()]


def internal_refs(db: Session, conv: Conversation, on: date) -> dict:
    """会话命中的场景，以及本店、本站点、当天生效、挂了这些场景的仅内部条款。"""
    from .retriever import exclusion_reason

    scenes = conversation_scenes(db, conv)
    wanted = {x["scene"] for x in scenes}
    rows = db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.store_id.in_((conv.store_id, "ALL")), KnowledgeChunk.audience == "INTERNAL_SOP",
        KnowledgeChunk.review_state == "PUBLISHED").order_by(KnowledgeChunk.priority, KnowledgeChunk.clause_id)).all()
    pinned, matched, seen = [], [], set()
    for c in rows:
        tags = c.scenes or []
        if not tags or c.clause_id in seen:
            continue
        if exclusion_reason(c, conv.store_id, conv.marketplace_id or "UNKNOWN", on, "INTERNAL_SOP"):
            continue
        hit = [s for s in tags if s in wanted]
        if ALWAYS in tags:
            pinned.append((c, [ALWAYS]))
        elif hit:
            matched.append((c, hit))
        else:
            continue
        seen.add(c.clause_id)
    from ..auth import current
    from .kb_docs import docs_brief

    user = current()
    # docs：条款引用的参考文档（编号、标题、链接）；敏感文档对非管理员没有链接，只显示标题
    item = lambda c, hit: {"clause_id": c.clause_id, "title": c.title,  # noqa: E731
                           "text": c.text.split("\n", 1)[1] if "\n" in c.text else c.text,
                           "priority": c.priority, "topic": c.topic, "scenes": hit, "pinned": hit == [ALWAYS],
                           "docs": docs_brief(db, c.doc_refs or [], user)}
    return {"scenes": scenes, "items": [item(c, h) for c, h in matched[:MAX_REFS]],
            "pinned": [item(c, h) for c, h in pinned], "more": max(0, len(matched) - MAX_REFS)}
