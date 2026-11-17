"""证据与关键陈述核对（第 11.2、16.3 节）。

模型自报的 claims 可能遗漏正文里的陈述，所以除了检查 evidence_ids 是否来自本轮，
还直接扫描 reply_text 中的金额、天数、日期，要求它们能在本轮证据原文里找到。
程序核对不了的陈述一律进入人工审核。
"""
from __future__ import annotations

import re

_MONEY = re.compile(r"[$£€]\s?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s?(?:USD|GBP|EUR|dollars?|pounds?|美元|英镑|欧元)", re.I)
_DAYS = re.compile(r"(\d+)\s*(?:[-–~]\s*\d+\s*)?(?:business |working |calendar )?(?:days?|hours?|个工作日|个?小时|天)", re.I)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DAY = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\b", re.I
)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _digits(s: str) -> str:
    return re.sub(r"[^\d.]", "", s).rstrip(".")


def check_claims(reply_text: str, claims: list[dict], allowed_evidence: set[str], corpus: str,
                 action: str, intent: str) -> list[str]:
    problems: list[str] = []
    for c in claims:
        if any(e not in allowed_evidence for e in c.get("evidence_ids", [])):
            problems.append("EVIDENCE_INVALID")
            break

    has_numbers = False
    for m in _MONEY.finditer(reply_text):
        has_numbers = True
        if _digits(m.group(0)) not in corpus:
            problems.append("UNVERIFIED_NUMBER")
            break
    for m in _DAYS.finditer(reply_text):
        has_numbers = True
        if m.group(1) not in corpus:
            problems.append("UNVERIFIED_NUMBER")
            break
    for m in _ISO_DATE.finditer(reply_text):
        has_numbers = True
        if m.group(0) not in corpus:
            problems.append("UNVERIFIED_NUMBER")
            break
    for m in _MONTH_DAY.finditer(reply_text):
        has_numbers = True
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month and f"-{month:02d}-{int(m.group(2)):02d}" not in corpus and m.group(0) not in corpus:
            problems.append("UNVERIFIED_NUMBER")
            break

    if action == "answer" and not claims and intent != "thanks_closing" and has_numbers:
        problems.append("CLAIMS_MISSING")
    return list(dict.fromkeys(problems))
