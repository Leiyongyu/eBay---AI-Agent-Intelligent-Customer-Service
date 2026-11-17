"""提示注入检测（第二批 B8，规则、不调模型）：买家消息 / 附件文字里“让 AI 忽略规则、换身份、直接退款 / 给退款码”这类话。
命中 → 风险原因 PROMPT_INJECTION，风险升到 L3（任何配置下都只能人工处理）。草稿照样起草给审核员参考。

另：fence() 把买家文本里像我们定界标签的字样（</buyer_message>、<current_task>、<system> 等）换成全角，
买家没法“提前结束”定界块、伪装成系统内容。"""
from __future__ import annotations

import re

PATTERNS = [re.compile(p, re.I) for p in (
    # 英语
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|system|original)?\s*"
    r"(?:instructions?|rules?|prompts?|guidelines?|directions?)\b",
    r"\byou\s+are\s+(?:now|no\s+longer)\s+(?:a|an|the|my)?\s*\w+",
    r"\b(?:system\s+prompt|developer\s+mode|jailbreak|dan\s+mode|prompt\s+injection)\b",
    r"\b(?:act|behave|pretend)\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:an?\s+)?(?:admin|administrator|developer|system|manager)\b",
    r"\b(?:give|send|issue)\s+me\s+(?:a\s+|the\s+)?(?:refund|voucher|coupon|discount)\s+code\b",
    r"\b(?:as\s+(?:the|an?)\s+(?:ai|assistant|customer\s+service|agent)),?\s+(?:you\s+(?:must|should|will)\s+)?"
    r"(?:refund|issue\s+a\s+refund|approve)\b",
    r"\brefund\s+me\s+(?:right\s+)?now\s+without\b",
    # 德语
    r"\b(?:ignorier\w*|vergiss)\s+(?:alle\s+|die\s+)?(?:vorherigen|obigen|bisherigen)?\s*(?:anweisungen|regeln|befehle)\b",
    r"\bdu\s+bist\s+(?:jetzt|nun)\s+(?:ein|eine|der|die)?\s*\w+",
    # 法语 / 意大利语 / 西班牙语
    r"\bignore[zr]?\s+(?:toutes\s+)?(?:les\s+)?(?:instructions|règles)\s+(?:précédentes|ci-dessus)\b",
    r"\bignora\s+(?:tutte\s+)?(?:le\s+)?(?:istruzioni|regole)\s+(?:precedenti|sopra)\b",
    r"\bignora\s+(?:todas\s+)?(?:las\s+)?(?:instrucciones|reglas)\s+(?:anteriores|previas)\b",
    # 中文
    r"忽略(?:以上|之前|前面|上面|所有)(?:的)?(?:规则|指令|要求|提示|设定)?",
    r"(?:你现在是|你的新身份是|扮演(?:管理员|系统|开发者))",
    r"(?:系统提示词|开发者模式|越狱)",
    r"(?:给我|发我)(?:一个)?(?:退款码|优惠码|兑换码)",
    r"你是客服[，,]?\s*(?:请|就)?(?:直接|马上|立刻)(?:给我)?退款",
)]
FENCE_TAGS = re.compile(r"<\s*/?\s*(?:buyer_message|current_task|runtime_context|tool_observations|company_knowledge|"
                        r"conversation_state|buyer_attachments|system|assistant|user|instructions?)\b", re.I)


def detect(text: str | None) -> list[str]:
    """命中的片段（最多 5 个）；空 = 没发现。"""
    hits: list[str] = []
    for rx in PATTERNS:
        for m in rx.finditer(text or ""):
            hits.append(m.group(0)[:80])
            if len(hits) >= 5:
                return hits
    return hits


def fence(text: str | None) -> str:
    """买家文本里像定界标签的字样换成全角“＜”，不让它冒充系统内容或提前结束定界块。"""
    return FENCE_TAGS.sub(lambda m: "＜" + m.group(0)[1:], text or "")
