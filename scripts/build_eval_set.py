"""抽黄金回归集（只读本地库，不调模型、不连外部系统）。

    python scripts/build_eval_set.py                     # 抽取 → data/eval/golden.jsonl（git 忽略），打印分层统计
    python scripts/build_eval_set.py --limit 300 --per-stratum 40
    python scripts/build_eval_set.py --export-review     # 另导出 data/eval/golden_review.csv，人工复核期望标签
    python scripts/build_eval_set.py --import-review data/eval/golden_review.csv   # 复核结果写回（label_source=human）

来源、脱敏、分层见 backend/app/evaluation/builder.py。已有 golden.jsonl 里人工复核过（label_source=human）的期望
在重抽时保留，不会被自动标签覆盖。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def export_review(cases: list[dict], path: Path) -> None:
    from app.evaluation import review

    path.write_text(review.export_csv(cases), encoding="utf-8")


def import_review(cases: list[dict], path: Path) -> int:
    """复核结果写回，并记审计 EVAL_LABELS_IMPORTED（操作人 cli）。"""
    from app.db import session_scope
    from app.evaluation import review

    done, errors = review.import_csv(cases, path.read_text(encoding="utf-8-sig"))
    if errors:
        print("这些行没写回：", *errors[:20], sep="\n  ")
    with session_scope() as db:
        review.audit(db, "cli", done, errors)
    return len(done)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--per-stratum", type=int, default=40)
    ap.add_argument("--no-synthetic", action="store_true", help="不把合成意图用例补进来")
    ap.add_argument("--out", default=None)
    ap.add_argument("--export-review", action="store_true")
    ap.add_argument("--import-review", default=None)
    a = ap.parse_args(argv)

    from app.evaluation import builder, golden

    out = Path(a.out) if a.out else golden.GOLDEN_FILE
    if a.import_review:
        cases = golden.load(out)
        n = import_review(cases, Path(a.import_review))
        golden.dump(cases, out)
        print(f"写回 {n} 条人工复核标签 → {out}")
        return 0

    from app.db import session_scope

    old = {c["case_id"]: c for c in golden.load(out) if c.get("label_source") == "human"}
    with session_scope() as db:
        cases, stats = builder.build(db, limit=a.limit, per_stratum=a.per_stratum, synthetic=not a.no_synthetic)
    for c in cases:  # 人工复核过的期望保留
        if c["case_id"] in old:
            c["expect"], c["label_source"], c["needs_review"] = old[c["case_id"]]["expect"], "human", False
            c["stratum"] = (c["expect"].get("intent") or ["other"])[0]
    bad = [(c["case_id"], e) for c in cases for e in golden.validate(c) if c["source"] != "synthetic" or "回放" not in e]
    golden.dump(cases, out)
    print(f"写出 {len(cases)} 条 → {out}")
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    d = stats["distribution"]
    print(f"如实统计：共 {len(cases)} 条；来源 {d['source']}；标签来源 {d['label_source']}；待复核 {d['needs_review']} 条")
    if bad:
        print(f"格式问题 {len(bad)} 条（前 10）：", bad[:10])
    if a.export_review:
        p = out.with_name("golden_review.csv")
        export_review(cases, p)
        print(f"复核表 → {p}（填好后 reviewed 列写 1，再用 --import-review 写回）")
    if len(cases) < 150:
        print(f"提示：只抽到 {len(cases)} 条，少于 150 条——已审核发出的 AI 草稿还很少，上线后每周重抽一次扩充。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
