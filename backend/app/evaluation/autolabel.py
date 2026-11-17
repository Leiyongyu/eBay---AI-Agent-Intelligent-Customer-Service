"""给没有标注的会话自动打“期望”（label_source=auto，needs_review=true，要人工复核）：
语言、主意图（关键词规则，英 / 德 / 法 / 意 / 西 / 中）、是否该转人工、必含要点（人工回复里出现、工具结果里也有的事实）。
只是起点：规则判不准的标 other，复核时改。"""
from __future__ import annotations

import re

LANG_WORDS = {
    "de": {"ich", "nicht", "und", "der", "die", "das", "ist", "mit", "bitte", "danke", "habe", "meine", "wann", "noch", "paket",
           "vielen", "dank", "diese", "bei", "mir", "sie", "für", "auf", "hallo", "fehler", "ware", "bestellung"},
    "fr": {"je", "pas", "et", "le", "la", "les", "est", "avec", "merci", "bonjour", "colis", "mon", "ma", "pour", "vous"},
    "it": {"non", "il", "la", "che", "è", "con", "grazie", "buongiorno", "pacco", "mio", "ho", "per", "ancora", "sono"},
    "es": {"no", "el", "la", "que", "es", "con", "gracias", "hola", "paquete", "mi", "tengo", "para", "todavía", "pedido"},
    "en": {"i", "the", "and", "is", "my", "not", "please", "thanks", "thank", "you", "order", "have", "when", "it", "item"},
}

# 意图关键词（命中越靠前的意图优先：售后 / 风险类排在物流类前面）
INTENT_RULES: list[tuple[str, re.Pattern]] = [(code, re.compile(p, re.I)) for code, p in [
    ("abuse_fraud_legal", r"\b(?:lawyer|anwalt|avocat|avvocato|abogado|police|polizei|fraud|betrug|scam|truffa|estafa)\b"),
    ("off_platform", r"\b(?:paypal\s+direct|whatsapp|outside\s+ebay|außerhalb\s+von\s+ebay|bank\s+transfer)\b"),
    ("feedback", r"\b(?:feedback|bewertung|évaluation|valutazione|valoración|negative|negativ)\b"),
    ("cancel_request", r"\b(?:cancel|stornier|annul|cancella|cancelar)\w*"),
    ("damaged_wrong", r"\b(?:damaged|broken|beschädigt|kaputt|cassé|endommagé|rotto|danneggiato|roto|dañado|wrong\s+item|"
                      r"falsch\w*\s+(?:artikel|teil)|missing\s+part|fehlt|manque|manca|falta)\b"),
    ("quality_issue", r"\b(?:not\s+working|doesn'?t\s+work|funktioniert\s+nicht|ne\s+fonctionne\s+pas|non\s+funziona|no\s+funciona|"
                      r"fault\s+code|fehlercode|leak|undicht|noise|geräusch)\b"),
    ("refund_status", r"\b(?:refund\s+status|where\s+is\s+my\s+refund|erstattung\s+(?:noch|wann)|rimborso\s+quando)\b"),
    ("return_refund", r"\b(?:return|refund|rücksend|zurück\w*|erstatt|rückgabe|retour|rembours|reso|rimborso|devoluci|reembolso)\w*"),
    ("address_change", r"\b(?:change\s+(?:the\s+)?address|adresse\s+ändern|changer\s+l'adresse|cambiare\s+indirizzo|cambiar\s+(?:la\s+)?dirección)\b"),
    ("invoice_vat", r"\b(?:invoice|rechnung|facture|fattura|factura|vat|mwst|ust)\b"),
    ("customs_duty", r"\b(?:customs|zoll|douane|dogana|aduana|import\s+tax|einfuhr)\w*"),
    ("delivery_delay", r"\b(?:not\s+(?:yet\s+)?(?:arrived|received)|still\s+(?:not|no)|noch\s+nicht\s+(?:angekommen|erhalten|da)|"
                       r"pas\s+(?:encore\s+)?reçu|non\s+(?:ancora\s+)?arrivato|no\s+(?:me\s+)?ha\s+llegado|delayed|verspätet)\b"),
    ("shipping_status", r"\b(?:tracking|track|sendungsverfolgung|where\s+is\s+my|wo\s+ist|suivi|spedizione|seguimiento|"
                        r"shipped|versendet|versand|expédié|spedito|enviado)\w*"),
    ("fitment", r"\b(?:fit|fits|passt|passend|compatible|kompatibel|compatibile|vin|fahrgestell)\w*"),
    ("install_help", r"\b(?:install|einbau|montage|torque|drehmoment|programm|coding|codier)\w*"),
    ("stock", r"\b(?:in\s+stock|available|verfügbar|lieferbar|disponible|disponibile|auf\s+lager)\b"),
    ("price_offer", r"\b(?:discount|rabatt|best\s+price|bester\s+preis|réduction|sconto|descuento|offer)\b"),
    ("shipping_cost", r"\b(?:shipping\s+cost|versandkosten|frais\s+de\s+port|spese\s+di\s+spedizione|gastos\s+de\s+envío)\b"),
    ("thanks_closing", r"^\W*(?:thanks?|thank\s+you|danke|merci|grazie|gracias|ok\W*thanks?|vielen\s+dank)\W*$"),
]]
HANDOFF_INTENTS = {"abuse_fraud_legal", "off_platform", "feedback"}  # 默认 L3：任何配置下都只能人工处理
# 回复里常见、可核对的事实：运单号、订单号、金额、承运商
FACT_PATTERNS = [
    re.compile(r"\b\d{2}-\d{5}-\d{5}\b"),                      # eBay 订单号
    re.compile(r"\b(?:1Z[0-9A-Z]{16}|[A-Z]{2}\d{9}[A-Z]{2}|\d{10,22})\b"),  # 运单号
    re.compile(r"\b(?:DHL|DPD|GLS|UPS|FedEx|Hermes|Royal Mail|Evri|Parcelforce|Colissimo|Poste Italiane|Correos|USPS)\b", re.I),
]
# 任何回复都不该出现的话（站外交易、诱导好评、私下退款等），合规规则之外再兜一层
GLOBAL_FORBIDDEN = ["whatsapp", "paypal.me", "outside ebay", "leave positive feedback", "positive feedback and",
                    "guaranteed refund", "100% refund guaranteed"]


def language(text: str) -> str | None:
    t = (text or "").lower()
    if sum("一" <= ch <= "鿿" for ch in t) >= 2:
        return "zh"
    words = re.findall(r"[a-zà-ÿäöüß']+", t)
    if not words:
        return None
    scores = {lang: sum(w in ws for w in words) for lang, ws in LANG_WORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def intents(text: str) -> list[str]:
    """命中的意图（按规则顺序）；什么都没命中返回 ["other"]。"""
    hit = [code for code, rx in INTENT_RULES if rx.search(text or "")]
    return list(dict.fromkeys(hit)) or ["other"]


def facts_in(reply: str, evidence: str) -> list[str]:
    """人工回复里出现、并且工具结果 / 买家消息里也有的事实（运单号、订单号、承运商），作为必含要点。"""
    out: list[str] = []
    low = (evidence or "").lower()
    for rx in FACT_PATTERNS:
        for m in rx.finditer(reply or ""):
            v = m.group(0)
            if v.lower() in low and v not in out:
                out.append(v)
    return out[:5]


def expect_for(buyer_text: str, human_reply: str, evidence: str, model_output: dict | None = None) -> dict:
    """自动期望：有模型输出且人工几乎没改时，以模型的意图 / action 为准；否则按关键词规则。"""
    got = intents(buyer_text)
    primary = (model_output or {}).get("intent") or got[0]
    action = (model_output or {}).get("action")
    handoff = primary in HANDOFF_INTENTS or action == "handoff"
    # 只有人工回复（没有当时的模型输出）：回放用人工回复当替身，转人工只能靠买家原文规则判断，自动标签不评转人工；
    # 合规接受 PASS / REVIEW（人工回复里提部分退款等本来就该人工确认），BLOCK 才算不过
    return {"intent": list(dict.fromkeys([primary, *[i for i in got if i != "other"][:2]])), "intent_not": [],
            "action": action, "handoff": handoff if model_output else None,
            "compliance": "PASS" if model_output else ["PASS", "REVIEW"], "must_include": facts_in(human_reply, evidence),
            "must_not_include": list(GLOBAL_FORBIDDEN)}
