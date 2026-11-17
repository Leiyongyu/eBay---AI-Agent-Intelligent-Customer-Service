"""黄金集期望标签的人工复核：导出 CSV（Excel 能直接打开）、复核后导入写回（label_source=human、needs_review=false，重抽时保留）。
页面接口只给管理员和售后；每次导入记审计 EVAL_LABELS_IMPORTED（谁、几条、哪些用例），命令行导入同样记（操作人 cli）。"""
from __future__ import annotations

import csv
import io

from sqlalchemy.orm import Session

from .golden import validate

FIELDS = ("case_id", "source", "label_source", "language", "buyer", "human_reply", "intent", "intent_not", "action", "handoff",
          "compliance", "must_include", "must_not_include", "reviewed")


def export_csv(cases: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, FIELDS)
    w.writeheader()
    for c in cases:
        e = c["expect"]
        comp = e.get("compliance")
        w.writerow({"case_id": c["case_id"], "source": c["source"], "label_source": c.get("label_source") or "",
                    "language": c.get("language"),
                    "buyer": "\n".join(m["text"] for m in c["input"]["inbound"])[:1000], "human_reply": (c.get("human_reply") or "")[:1000],
                    "intent": ",".join(e.get("intent") or []), "intent_not": ",".join(e.get("intent_not") or []),
                    "action": e.get("action") or "", "handoff": "" if e.get("handoff") is None else str(e["handoff"]).lower(),
                    "compliance": ",".join(comp) if isinstance(comp, list) else (comp or ""),
                    "must_include": " | ".join(e.get("must_include") or []),
                    "must_not_include": " | ".join(e.get("must_not_include") or []),
                    "reviewed": "1" if c.get("label_source") == "human" else ""})
    return "﻿" + buf.getvalue()  # 带 BOM：Excel 打开中文不乱码


def import_csv(cases: list[dict], text: str) -> tuple[list[str], list[str]]:
    """把 reviewed=1 的行写回 cases（就地改）。返回 (写回的 case_id, 问题)；有问题的行不写回。"""
    by_id = {c["case_id"]: c for c in cases}
    done, errors = [], []

    def split(v, sep):
        return [x.strip() for x in (v or "").split(sep) if x.strip()]

    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        cid = (row.get("case_id") or "").strip()
        if (row.get("reviewed") or "").strip().lower() not in ("1", "y", "yes", "true", "是"):
            continue
        c = by_id.get(cid)
        if c is None:
            errors.append(f"{cid}：黄金集里没有这条")
            continue
        comp = split(row.get("compliance"), ",")
        new_expect = {"intent": split(row.get("intent"), ","), "intent_not": split(row.get("intent_not"), ","),
                      "action": (row.get("action") or "").strip() or None,
                      "handoff": None if not (row.get("handoff") or "").strip()
                      else row["handoff"].strip().lower() in ("true", "1", "yes", "是"),
                      "compliance": None if not comp else (comp[0] if len(comp) == 1 else comp),
                      "must_include": split(row.get("must_include"), "|"),
                      "must_not_include": split(row.get("must_not_include"), "|")}
        trial = {**c, "expect": new_expect, "label_source": "human"}
        errs = validate(trial)
        if errs:
            errors.append(f"{cid}：{'；'.join(errs)}")
            continue
        c["expect"], c["label_source"], c["needs_review"] = new_expect, "human", False
        c["stratum"] = (new_expect["intent"] or ["other"])[0]
        done.append(cid)
    return done, errors


def audit(db: Session, actor: str, done: list[str], errors: list[str]) -> None:
    from ..models import AuditEvent

    db.add(AuditEvent(actor=actor[:64], action="EVAL_LABELS_IMPORTED",
                      detail={"count": len(done), "case_ids": done[:100], "errors": errors[:20]}))


def distribution(cases: list[dict]) -> dict:
    """来源 × 标签来源的分布（报告、面试、复盘用）。"""
    from collections import Counter

    return {"source": dict(Counter(c.get("source") for c in cases)),
            "label_source": dict(Counter(c.get("label_source") or "auto" for c in cases)),
            "source_x_label": {f"{s}/{l}": n for (s, l), n in
                               Counter((c.get("source"), c.get("label_source") or "auto") for c in cases).items()},
            "needs_review": sum(1 for c in cases if c.get("needs_review"))}
