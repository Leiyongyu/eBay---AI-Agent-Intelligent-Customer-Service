"""风险分级与发送闸门（第 3.2 节）。风险等级由程序决定，不听模型自报。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..intents import RISK_ORDER, RULE_VERSION, default_risk, max_risk

# 固定规则：买家原文命中即提升风险（第 14.2 节“固定规则可以直接识别必须人工处理的场景”）
_L3_TEXT = re.compile(
    r"\b(lawyer|attorney|lawsuit|sue|legal action|police|fraud|scam|chargeback|injur\w*|accident|crash\w*)\b"
    r"|律师|起诉|诉讼|报警|诈骗|受伤|事故",
    re.I,
)
_L2_TEXT = re.compile(
    r"\b(refund|return|cancel\w*|change (?:my |the )?(?:shipping )?address|broken|damaged|cracked|wrong item|missing parts?|not as described)\b"
    r"|退款|退货|取消|改地址|修改地址|损坏|破损|错发|缺件",
    re.I,
)

REASON_LABELS = {
    "AGENT_NOT_READY": "模型未配置，需人工撰写",
    "KNOWLEDGE_OVER_BUDGET": "适用知识超出全量注入预算",
    "RAG_NOT_READY": "知识检索未就绪（pgvector/向量模型/索引）",
    "CONTEXT_OVER_BUDGET": "提示超出输入预算（不能截掉本次买家问题）",
    "TOOL_BUDGET_EXCEEDED": "工具调用超出预算",
    "TOOL_ERROR": "业务数据查询失败",
    "DEADLINE_EXCEEDED": "超出编排时限",
    "LLM_ERROR": "模型调用失败",
    "LLM_STORE_OFF": "非对接店铺，不用大模型起草（人工处理）",
    "OUTPUT_INVALID": "模型输出无法解析",
    "RULE_L3_TEXT": "买家原文命中 L3 规则（法律/安全/诈骗）",
    "RULE_L2_TEXT": "买家原文涉及退款/退货/取消/地址/破损",
    "OPEN_CASE": "存在未结售后 case",
    "HUMAN_HANDOFF": "会话已人工接管",
    "BUYER_BLACKLISTED": "黑名单买家：AI 不起草回复，请人工处理",
    "REPLIED_ON_EBAY": "同事已在 eBay 后台回复，系统草稿作废",
    "HUMAN_FOLLOWING": "同事正在跟进这个会话（上一条是人工回复）：AI 草稿只作参考，请跟进的同事结合自己和买家谈过的内容核对后再发",
    "MARKETPLACE_UNKNOWN": "站点未知",
    "MODEL_REQUESTED_HUMAN": "模型建议转人工",
    "INTENT_UNCERTAIN": "AI 看不准买家指的是哪件事 / 哪个订单（意图把握度低），已改为礼貌确认，请核对",
    "MISSING_FACTS": "缺少必要事实",
    "LANGUAGE_MISMATCH": "回复语言和买家最新来信不一致",
    "INVOICE_EMAIL_RECEIVED": "买家发来接收发票的邮箱：请在会话“发票”里核对订单、审核后发邮件",
    "INVOICE_VAT_HUMAN": "买家要增值税发票：系统不开增值税发票，请人工开具后发给买家",
    "INVOICE_AUTO_FAILED": "普通发票没能自动发出（见发票页备注）：回复里说“已发送”的请改，处理好发票再回复",
    "INVOICE_NEEDS_ORDER": "买家给了邮箱但有多笔订单或查不到订单，发票没发出；回复应请买家说明订单号，请核对回复（不能说已发出）",
    "KNOWLEDGE_CONFLICT": "知识条款相互冲突",
    "EVIDENCE_INVALID": "引用了本次不存在的证据",
    "CLAIMS_MISSING": "回复有事实陈述但没有绑定证据",
    "UNVERIFIED_NUMBER": "金额/日期/天数在证据中找不到",
    "COMPLIANCE_BLOCK": "合规硬阻断",
    "COMPLIANCE_REVIEW": "合规需人工确认",
    "RISK_NOT_L0": "风险等级超过自动发送上限（AUTO_SEND_MAX_RISK）",
    "INTENT_NOT_WHITELISTED": "意图不在自动发送白名单",
    "AUTO_SEND_DISABLED": "自动发送未开启（P2 全部人工审核）",
    "OLD_MESSAGE": "买家来信已超过 24 小时（补拉到的旧消息）：只起草，人工审核后再发",
    "STORE_AUTO_SEND_OFF": "店铺未开启自动发送",
    "NO_KB_EVIDENCE": "回复没有可靠依据（没引用知识库、商品信息或查询结果）",
    "AUTO_REPLY_STREAK": "AI 已连续自动回复多次（达到 AUTO_REPLY_MAX_STREAK），这次交人工",
    "HAS_ATTACHMENTS": "买家发了附件，需人工查看原图",
    "ATTACHMENT_NOT_READ": "有附件未被读取（未开启识图、下载失败或非图片），AI 草稿没看过这些附件",
    "IMAGE_DAMAGE_OBSERVED": "图片中观察到外观异常（破损/变形等），按售后问题人工处理",
    "TRACKING_EXCEPTION": "物流轨迹异常（丢失、损坏、退件、扣关、投递失败或运输过久），需人工处理",
    "TRACKING_DELIVERED_NOT_RECEIVED": "轨迹显示已签收但买家说没收到，按未收到货纠纷人工处理",
    "TRACKING_NOT_FOUND": "查不到物流信息（没有跟踪号或轨迹查不到），需人工核实",
    "TRACKING_STALLED": "物流轨迹多天未更新（达到 LOGISTICS_STALL_DAYS），需人工判断是否补发",
    "TRACKING_LOST_DAMAGED": "物流轨迹显示损坏、丢失、退回或运输过久，需人工判断是否补发",
    "EMPTY_PARCEL": "买家说收到的是空包裹，需人工判断是否补发",
    "TRACKING_PICKUP_OVERDUE": "包裹在取件点放太久还没取，快到期退回，需人工提醒买家或安排",
    "SHIP_LATE": "已过 eBay 承诺的最晚发货日还没发货，需人工联系仓库",
    "TRACKING_RESHIP_REVIEW": "物流轨迹长时间未更新（达到 LOGISTICS_RESHIP_DAYS），需人工判断是否补发",
    "RESHIP_REQUESTED": "买家要求补发/重发/换货，由人工决定",
}

# 这几项只是“为什么没进自动发送”的闸门说明，不代表草稿有问题；工作台用灰色显示
GATE_INFO_REASONS = {"RISK_NOT_L0", "INTENT_NOT_WHITELISTED", "STORE_AUTO_SEND_OFF", "AUTO_SEND_DISABLED",
                     "NO_KB_EVIDENCE", "AUTO_REPLY_STREAK"}


@dataclass
class GateInput:
    intents: list[str]
    buyer_text: str
    open_cases: list[dict] = field(default_factory=list)
    handoff_state: str = "NONE"
    marketplace_id: str = "UNKNOWN"
    action: str | None = None
    needs_human: bool = False
    missing_facts: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # 生成前/校验中发现的问题原因码
    compliance_result: str = "PASS"
    auto_send_enabled: bool = False
    store_auto_send_enabled: bool = False
    auto_send_intents: set[str] = field(default_factory=set)
    # 这些 L1 意图在“只向买家索要信息”（action=clarify，如适配咨询先要 VIN）时也可自动发；给出结论的仍要人工
    auto_clarify_intents: set[str] = field(default_factory=set)
    kb_evidence: int = 0  # 回复引用的知识库条款数（kb:…）；自动发送要求至少 1 条（或下面的商品信息依据）
    # 商品参数类意图（AUTO_SEND_LISTING_INTENTS，可含 L1 如运费、发货时效）按商品信息（listing:…）作答时也可自动发
    listing_evidence: int = 0
    auto_listing_intents: set[str] = field(default_factory=set)
    previous_auto_reply: bool = False  # 会话末尾连续的 AI 自动回复已达上限（AUTO_REPLY_MAX_STREAK）
    auto_max_risk: str = "L0"  # 自动发送允许的最高风险等级（L0 / L1）
    auto_exclude_intents: set[str] = field(default_factory=set)  # 即使白名单是 * 也不自动发的意图
    evidence_mode: str = "kb"  # kb：要知识库条款；any：任一依据或没有事实陈述
    other_evidence: int = 0  # 引用的其他依据数（订单、物流、售后等查询结果）
    claims: int = 0  # 回复里的事实陈述条数
    attachments: int = 0  # 本次任务买家消息的附件数
    attachments_not_read: int = 0  # 其中没被视觉模型读过的
    image_damage: bool = False  # 图片观察到外观异常


@dataclass
class GateDecision:
    risk_level: str
    decision: str  # AUTO_ELIGIBLE / REVIEW_REQUIRED
    reasons: list[str]
    rule_version: str = RULE_VERSION


def evaluate(g: GateInput) -> GateDecision:
    reasons = list(dict.fromkeys(g.problems))
    risk = max_risk(*(default_risk(i) for i in g.intents)) if g.intents else "L1"

    if _L3_TEXT.search(g.buyer_text):
        risk = max_risk(risk, "L3")
        reasons.append("RULE_L3_TEXT")
    elif _L2_TEXT.search(g.buyer_text):
        risk = max_risk(risk, "L2")
        reasons.append("RULE_L2_TEXT")
    if any(c.get("status") not in ("CLOSED", "RESOLVED") for c in g.open_cases):
        risk = max_risk(risk, "L2")
        reasons.append("OPEN_CASE")
    # 依赖图片的来信至少 L1、一律人工审核；看到破损等外观异常按售后问题 L2 处理（第 20 章）。
    # 不会因为图片内容自动同意退款、补发，也不会据此自动发送。
    if g.attachments:
        risk = max_risk(risk, "L1")
        reasons.append("HAS_ATTACHMENTS")
        if g.attachments_not_read:
            reasons.append("ATTACHMENT_NOT_READ")
    if g.image_damage:
        risk = max_risk(risk, "L2")
        reasons.append("IMAGE_DAMAGE_OBSERVED")
    if g.handoff_state == "HUMAN":
        reasons.append("HUMAN_HANDOFF")
    if g.marketplace_id == "UNKNOWN":
        reasons.append("MARKETPLACE_UNKNOWN")
    if g.action == "handoff" or g.needs_human:
        reasons.append("MODEL_REQUESTED_HUMAN")
    # 追问（clarify）本来就是向买家要缺的信息：模型把要问的内容写进 missing_facts 不算“缺事实”
    if g.missing_facts and g.action != "clarify":
        reasons.append("MISSING_FACTS")
    if g.conflicts:
        reasons.append("KNOWLEDGE_CONFLICT")
    if g.compliance_result == "BLOCK":
        reasons.append("COMPLIANCE_BLOCK")
    elif g.compliance_result == "REVIEW":
        reasons.append("COMPLIANCE_REVIEW")
    clarify_ok = (risk == "L1" and g.action == "clarify" and bool(g.intents)
                  and set(g.intents) <= g.auto_clarify_intents)
    listing_ok = (risk in ("L0", "L1") and g.action == "answer" and g.listing_evidence > 0 and bool(g.intents)
                  and set(g.intents) <= g.auto_listing_intents)
    # 风险上限（AUTO_SEND_MAX_RISK，默认 L0）；L2/L3（退款、取消、改地址、破损错发、纠纷等）任何配置下都不自动发
    if RISK_ORDER[risk] > RISK_ORDER[g.auto_max_risk] and not clarify_ok and not listing_ok:
        reasons.append("RISK_NOT_L0")

    def intent_ok(i: str) -> bool:  # 白名单含 * 表示全部意图，排除 AUTO_SEND_EXCLUDE_INTENTS
        if i in g.auto_exclude_intents:
            return False
        return "*" in g.auto_send_intents or i in g.auto_send_intents

    allowed = {i for i in g.intents if intent_ok(i)} | (g.auto_clarify_intents if clarify_ok else set())         | (g.auto_listing_intents if listing_ok else set())
    if g.intents and not set(g.intents) <= allowed:
        reasons.append("INTENT_NOT_WHITELISTED")
    # 依据：kb = 必须引用知识库条款（或商品参数类按商品信息）；any = 引用任一依据（知识库、商品信息、订单物流等查询结果），
    # 或者回复里没有事实陈述（寒暄、致谢、请买家补充信息）
    if g.evidence_mode == "any":
        grounded = g.kb_evidence or g.listing_evidence or g.other_evidence or not g.claims
    else:
        grounded = g.kb_evidence or listing_ok
    if not grounded:
        reasons.append("NO_KB_EVIDENCE")
    if g.previous_auto_reply:
        reasons.append("AUTO_REPLY_STREAK")
    if not g.store_auto_send_enabled:
        reasons.append("STORE_AUTO_SEND_OFF")
    if not g.auto_send_enabled:
        reasons.append("AUTO_SEND_DISABLED")

    reasons = list(dict.fromkeys(reasons))
    decision = "REVIEW_REQUIRED" if reasons else "AUTO_ELIGIBLE"
    return GateDecision(risk, decision, reasons)
