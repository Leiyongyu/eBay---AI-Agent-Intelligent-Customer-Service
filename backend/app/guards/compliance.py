"""平台消息合规硬校验（第 18.1 节）。自动与人工外发共用同一个 ComplianceChecker。

结果：PASS / REVIEW / BLOCK。BLOCK 不能由审核员绕过，只能改文本后重检。
当前全部基于规则（正则 + 规范化 + 否定语境），方案中的“模型分类”尚未接入；
模型分类只能增加命中，不能撤销规则命中。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..config import get_settings

RULES_VERSION = "compliance-2026.11.09-2"

# 承运商官网：给买家物流信息时会带上（发货通知、AI 回复里的 carrier_website），各站点本地承运商都要在内
CARRIER_DOMAINS = {
    # 国际 / 美国 / 加拿大 / 澳洲
    "dhl.com", "ups.com", "fedex.com", "usps.com", "tnt.com", "canadapost-postescanada.ca", "canadapost.ca",
    "auspost.com.au", "17track.net", "yuntrack.com", "yunexpress.com",
    # 德国 / 奥地利 / 瑞士
    "dhl.de", "deutschepost.de", "dpd.de", "dpd.com", "dpd.at", "gls-pakete.de", "gls-group.com", "gls-group.eu",
    "gls-austria.com", "hermes.de", "myhermes.de", "hermesworld.com", "post.at", "post.ch", "dhl.at",
    # 英国
    "royalmail.com", "parcelforce.com", "dpd.co.uk", "evri.com", "myhermes.co.uk", "yodel.co.uk", "inpost.co.uk",
    "dhl.co.uk", "dhlparcel.co.uk", "dx.co.uk", "parcel2go.com",
    # 法国
    "laposte.fr", "colissimo.fr", "chronopost.fr", "dpd.fr", "gls-france.com", "colisprive.com", "mondialrelay.fr",
    "dhl.fr",
    # 意大利
    "poste.it", "brt.it", "gls-italy.com", "sda.it", "nexive.it", "dhl.it",
    # 西班牙
    "correos.es", "seur.com", "mrw.es", "gls-spain.es", "nacex.es", "ctt.es", "dhl.es",
    # 荷兰 / 比利时 / 波兰
    "postnl.nl", "postnl.com", "dhlparcel.nl", "bpost.be", "inpost.pl", "poczta-polska.pl",
}

BUILTIN_URL_WHITELIST = {
    # eBay 自有域名
    "ebay.com", "ebay.co.uk", "ebay.de", "ebay.fr", "ebay.it", "ebay.es", "ebay.com.au", "ebay.ca", "ebay.at",
    *CARRIER_DOMAINS,
    # 售后视频：eBay 消息发不了视频，请买家传到 WeTransfer 后把订单号和链接发来（知识库 POL-ASV-001）
    "wetransfer.com", "we.tl",
}

# 否定/拒绝语境：命中词出现在这些词附近时，多半是在婉拒（如“我们不能接受站外付款”），降级为 REVIEW
_NEGATION = re.compile(
    r"\b(not|cannot|can't|can not|unable|won't|don't|do not|never|only (?:through|via|on) ebay|no)\b"
    r"|不能|无法|不接受|不提供|不会|只能|仅限|请勿|不要",
    re.I,
)

_EMAIL = re.compile(r"[a-z0-9._%+-]+\s*(?:@|\(at\)|\[at\]|\sat\s)\s*[a-z0-9-]+\s*(?:\.|\(dot\)|\[dot\]|\sdot\s)\s*[a-z]{2,}", re.I)
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|me|co|uk|cn|de|info|biz|shop|store|link|ly)\b(?:/[^\s]*)?", re.I)
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s().\-]{7,}\d(?![\w-])")
# 汽配零件号（如大众 OE 号 “028 103 373 J”、奔驰 “A211 740 00 36”）常是成组数字，不能当成电话：
# 前面 40 字符内有零件号字样，或后面紧跟 1–3 个字母的后缀，就按零件号处理
_PART_NUMBER_CONTEXT = re.compile(
    r"\b(?:OE|OEM|part\s*(?:no|number|#)|teilenummer|teilnummer|originalteilnummer|ersatzteilnummer|"
    r"artikelnummer|referenznummer|num[ée]ro\s+de\s+pi[eè]ce|r[ée]f[ée]rence|codice|n[°º]|nr\.?)\b|零件号|原厂号|配件号|编号",
    re.I,
)
_LETTER_SUFFIX = re.compile(r"^\s?[A-Z]{1,3}\b")
_EBAY_ORDER_ID = re.compile(r"\d{2}-\d{5}-\d{5}")
_INVOICE_NO = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-\d{5}(?:-\d{1,2})?")  # 本系统发票号 20261030-32244
_YEAR_RANGE_OR_DATE = re.compile(r"(?:19|20)\d{2}\s*[-–/]\s*(?:(?:19|20)\d{2}|\d{1,2}\s*[-/]\s*\d{1,2})")
# 日在前的日期（德式 03.11.2026、欧式 03/11/2026），可带时间（“03.11.2026 03:59”“02.11.2026, um 21:25 Uhr”）：
# 物流轨迹时间常这样写，查电话号码前先遮掉，否则“03.11.2026 03”会凑够 10 位数字被当成电话
_DAY_FIRST_DATE_TIME = re.compile(
    r"(?<!\d)\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}"
    r"(?:,?\s*(?:um|at|alle|à|a las)?\s*\d{1,2}[:h]\d{2}(?::\d{2})?(?:\s*Uhr)?)?(?!\d)", re.I)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_MESSENGER = re.compile(r"whats\s*app|wechat|微信|telegram|skype|line\s*id|signal app|instagram|facebook|messenger|discord|kakao", re.I)
_HANDLE = re.compile(r"@[a-z0-9_.]{3,}|\b(?:id|handle|account|number|号)\s*[:：]?\s*[a-z0-9_.+-]{4,}", re.I)
_OFF_PLATFORM = re.compile(
    r"friends\s*(?:and|&)\s*family|pay (?:me|us) directly|outside (?:of )?ebay|off[- ]ebay|bank transfer|wire transfer"
    r"|zelle|venmo|cash\s*app|western union|站外(?:付款|交易)|私下(?:交易|付款)|绕过\s*ebay",
    re.I,
)
_FEEDBACK_TRADE = re.compile(
    r"(?:positive|good|5[- ]?star|five[- ]?star|remove|revise|change|delete)\s+(?:your\s+|the\s+|a\s+)?(?:feedback|review|rating)"
    r".{0,80}?(?:refund|discount|coupon|gift|cash ?back|money|compensat)"
    r"|(?:refund|discount|coupon|gift|cash ?back|compensat).{0,80}?(?:positive|good|5[- ]?star|five[- ]?star)\s+(?:feedback|review|rating)"
    r"|好评.{0,20}(?:返现|退款|折扣|补偿|红包)|(?:返现|退款|折扣|补偿|红包).{0,20}(?:好评|改评|删评|撤评)",
    re.I | re.S,
)
# 引导买家撤销 eBay 退货申请、改选退货原因（知识库 CS-RISK-002）：改原因、说原因影响店铺、撤销退货后私下给标签/退款
_RETURN_STEERING = re.compile(
    r"(?:change|select|choose|pick|modify|update)\w*\s+(?:the\s+|your\s+|a\s+|another\s+|different\s+)*(?:return\s+)?reason"
    r"|reason\s+(?:you\s+)?(?:selected|chose|chosen|picked).{0,80}?(?:affect|impact|harm|damage|hurt|negative|reputation|rating)"
    r"|(?:cancel|close|withdraw)\w*\s+(?:the\s+|your\s+|this\s+)?return(?:\s+request)?.{0,120}?"
    r"(?:we\s+will|we'll|then|after\s+that|afterwards).{0,80}?(?:label|refund)"
    r"|(?:rückgabe|rücksende|retouren)?grund\s+(?:\w+\s+){0,3}(?:ändern|wechseln|anpassen)"
    r"|grund.{0,60}?(?:beeinträchtig|schade[nt]|negativ|auswirk)"
    r"|ändern\w*\s+(?:sie\s+)?(?:bitte\s+)?(?:\w+\s+){0,2}(?:rückgabe|rücksende|retouren)grund"
    r"|(?:rücksendeanfrage|rückgabeanfrage|rücksendung|rückgabe)\w*\s+(?:\w+\s+){0,3}stornier.{0,120}?(?:etikett|erstatt)"
    r"|stornier\w*\s+(?:sie\s+)?(?:bitte\s+)?(?:die|ihre)\s+(?:rücksendeanfrage|rückgabeanfrage|rücksendung|rückgabe).{0,120}?(?:etikett|erstatt)"
    r"|退货原因.{0,10}(?:改|换|修改|选择)|(?:撤销|取消)(?:退货申请|退货请求).{0,40}(?:标签|退款)",
    re.I | re.S,
)
# 提出保留商品+部分退款、退款比例（知识库 CS-RISK-004）：比例和金额只能由人定，AI 草稿命中一律人工审核
_REFUND_OFFER = re.compile(
    r"\d{1,2}\s?%\s*(?:of\s+the\s+(?:price|payment|amount)\s+)?(?:as\s+(?:a\s+)?)?(?:refund|partial\s+refund|compensation|discount"
    r"|rückerstattung|erstattung|entschädigung|rabatt|remboursement|rimborso|reembolso)"
    r"|(?:refund|erstatten|rückerstatten)\w*\s+(?:you\s+|ihnen\s+)?\d{1,2}\s?%"
    r"|keep\s+(?:it|the\s+item|the\s+product|this(?:\s+item)?).{0,80}?(?:refund|compensation|discount)"
    r"|behalten.{0,80}?(?:erstatt|entschädigung|rabatt|nachlass)"
    r"|partial\s+refund|teilerstattung|teilweise\s+(?:rück)?erstattung|部分退款|退.{0,4}\d{1,2}\s?%",
    re.I | re.S,
)
_INTERNAL = re.compile(
    r"\b(?:our cost|cost price|unit cost|profit margin|margin|wholesale cost|internal (?:policy|sop|threshold))\b"
    r"|成本价|进货价|毛利|利润率|内部(?:规定|流程|SOP|阈值)|补偿(?:上限|阈值|权限)",
    re.I,
)


@dataclass
class ComplianceHit:
    rule: str
    severity: str  # BLOCK / REVIEW
    start: int
    end: int
    text: str
    note: str = ""


@dataclass
class ComplianceResult:
    result: str
    hits: list[ComplianceHit] = field(default_factory=list)
    rules_version: str = RULES_VERSION

    def to_dict(self) -> dict:
        return {
            "result": self.result,
            "rules_version": self.rules_version,
            "hits": [h.__dict__ for h in self.hits],
        }


def _normalize(text: str) -> str:
    # 全角转半角、去掉零宽字符；保持长度基本不变，命中位置仍可在原文上高亮
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[​-‏⁠﻿]", "", text)


def _negated(text: str, start: int, end: int, window: int = 60) -> bool:
    return bool(_NEGATION.search(text[max(0, start - window): min(len(text), end + window)]))


def _domain_allowed(url: str, whitelist: set[str]) -> bool:
    host = re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower()).split("/")[0].split(":")[0]
    return any(host == d or host.endswith("." + d) for d in whitelist)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


class ComplianceChecker:
    def __init__(self, extra_whitelist: set[str] | None = None):
        extra = {d.strip().lower() for d in (get_settings().compliance_extra_url_whitelist or "").split(",") if d.strip()}
        self.whitelist = BUILTIN_URL_WHITELIST | extra | (extra_whitelist or set())

    def check(self, text: str, allowed_tokens: set[str] | None = None) -> ComplianceResult:
        """allowed_tokens：本次证据里出现的订单号、追踪号等，避免把它们误判为电话号码。"""
        t = _normalize(text or "")
        allowed_digits = {re.sub(r"\D", "", tok) for tok in (allowed_tokens or set())}
        allowed_digits.discard("")
        hits: list[ComplianceHit] = []

        def add(rule, severity, m, note=""):
            hits.append(ComplianceHit(rule, severity, m.start(), m.end(), m.group(0), note))

        cfg = get_settings()
        own = {e.strip().lower() for e in (cfg.aftersales_public_email, cfg.invoice_smtp_user) if e}
        for m in _EMAIL.finditer(t):
            if re.sub(r"\s+", "", m.group(0)).lower() in own:
                continue  # 本店售后邮箱、发票发件邮箱：负责人允许告诉买家
            add("off_platform_contact.email", "BLOCK", m)
        url_spans: list[tuple[int, int]] = []
        for m in _URL.finditer(t):
            if "@" in t[max(0, m.start() - 1): m.start() + 1]:
                continue  # 邮箱的域名部分已在上面命中
            url_spans.append((m.start(), m.end()))
            if not _domain_allowed(m.group(0), self.whitelist):
                add("external_link", "BLOCK", m, "域名不在白名单")

        def in_url(m) -> bool:
            return any(s <= m.start() < e for s, e in url_spans)

        for m in _CARD.finditer(t):
            if in_url(m):
                continue
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits) and digits not in allowed_digits:
                add("pii.card_number", "BLOCK", m, "疑似银行卡号回显")
        no_dates = _DAY_FIRST_DATE_TIME.sub(lambda d: "x" * len(d.group(0)), t)  # 等长替换，命中位置不变
        for m in _PHONE.finditer(no_dates):
            if in_url(m):
                continue  # 链接里的数字（刊登号等）由外链规则负责
            s = m.group(0)
            digits = re.sub(r"\D", "", s)
            if len(digits) < (8 if s.startswith("+") else 9):
                continue
            if len(digits) > 15:
                continue  # 国际电话号码最多 15 位（E.164），更长的是追踪号、条码等
            if _EBAY_ORDER_ID.fullmatch(s.strip()):
                continue  # eBay 订单号 12-34567-89012
            if _INVOICE_NO.fullmatch(s.strip()):
                continue  # 本系统发票号：开票日期-订单号后 5 位
            if _YEAR_RANGE_OR_DATE.search(s):  # 2016-2021、2026-10-23 这类年款/日期
                continue
            if digits in allowed_digits or any(digits in a for a in allowed_digits):
                continue
            if _PART_NUMBER_CONTEXT.search(t[max(0, m.start() - 40): m.start()]) or \
                    _LETTER_SUFFIX.match(t[m.end(): m.end() + 5]) or re.search(r"[A-Za-z]$", t[max(0, m.start() - 1): m.start()]):
                continue  # 零件号
            if any(h.rule == "pii.card_number" and h.start <= m.start() < h.end for h in hits):
                continue
            add("off_platform_contact.phone", "BLOCK", m)
        for m in _MESSENGER.finditer(t):
            follow = t[m.end(): m.end() + 40]
            if _HANDLE.search(follow) or re.match(r"\s*[:：]?\s*\+?\d{6,}", follow):
                add("off_platform_contact.messenger", "BLOCK", m, "即时通讯账号")
            else:
                add("off_platform_contact.messenger_mention", "REVIEW", m, "提到站外通讯工具，确认没有引导站外联系")
        for m in _OFF_PLATFORM.finditer(t):
            if _negated(t, m.start(), m.end()):
                add("off_platform_trade", "REVIEW", m, "出现在婉拒语境中，请人工确认")
            else:
                add("off_platform_trade", "BLOCK", m)
        for m in _FEEDBACK_TRADE.finditer(t):
            if _negated(t, m.start(), m.end()):
                add("feedback_manipulation", "REVIEW", m, "出现在婉拒语境中，请人工确认")
            else:
                add("feedback_manipulation", "BLOCK", m)
        for m in _INTERNAL.finditer(t):
            add("internal_info", "REVIEW", m)
        for m in _RETURN_STEERING.finditer(t):
            if _negated(t, m.start(), m.end()):
                add("return_reason_steering", "REVIEW", m, "出现在否定语境中，请人工确认")
            else:
                add("return_reason_steering", "BLOCK", m, "不得让买家撤销退货申请或改选退货原因")
        for m in _REFUND_OFFER.finditer(t):
            add("refund_offer", "REVIEW", m, "部分退款、退款比例只能由人定")

        hits.sort(key=lambda h: h.start)
        if any(h.severity == "BLOCK" for h in hits):
            result = "BLOCK"
        elif hits:
            result = "REVIEW"
        else:
            result = "PASS"
        return ComplianceResult(result, hits)
