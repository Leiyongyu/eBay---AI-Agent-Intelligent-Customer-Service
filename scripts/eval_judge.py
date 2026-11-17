"""第二层 LLM 评审（真实调用模型，要用户同意才跑）：用轻量档模型给 AI 草稿的忠实度 / 语气 / 越权承诺打分（1–5）。

    python scripts/eval_judge.py                    # 默认只抽样、估算 token，不调模型
    python scripts/eval_judge.py --live             # 真实评审（先打印条数和预计 token，输入 yes 才开始）
    python scripts/eval_judge.py --live --yes --sample 5 --hours 48
    python scripts/eval_judge.py --golden --live    # 从黄金集里有当时模型输出的用例抽（最近没有 AI 草稿时）

结果记进 eval_runs（kind=judge），用量记进用量统计（purpose=eval_judge）；报告 data/eval/runs/judge-<时间>.json。
每晚自动跑：.env 里 EVAL_JUDGE_ENABLED=true（同样要用户同意后再开）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from app.config import get_settings

    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=s.eval_judge_sample)
    ap.add_argument("--hours", type=int, default=s.eval_judge_hours)
    ap.add_argument("--golden", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args(argv)

    from app.db import session_scope
    from app.evaluation import golden, judge
    from app.knowledge.retriever import estimate_tokens

    with session_scope() as db:
        items = (judge.sample_golden(golden.load(), a.sample, a.seed) if a.golden
                 else judge.sample_drafts(db, a.sample, a.hours, a.seed))
        if not items:
            print("没有可评审的草稿" + ("（黄金集里没有带模型输出的用例）" if a.golden else f"（最近 {a.hours} 小时没有 AI 草稿；可加 --golden）"))
            return 0
        est = sum(estimate_tokens(m.content) for it in items for m in judge.messages(it)) + 200 * len(items)
        print(f"抽到 {len(items)} 条，预计约 {est} 输入 token（{s.eval_judge_tier} 档模型，每条 1 次调用）")
        if not a.live:
            print("没有 --live：只抽样、估算，不调模型。")
            return 0
        if not a.yes and input("会真实调用模型，输入 yes 继续：").strip().lower() != "yes":
            print("已取消")
            return 1
        res = judge.run(db, items, trigger="manual", actor="eval_judge")
    out = ROOT / "data" / "eval" / "runs" / f"judge-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    sm = res["summary"]
    print(f"评了 {sm['judged']}/{sm['cases']} 条，平均：{sm['avg']}，综合 {sm['pass_rate']}；eval_runs #{res['run_id']}；报告 {out}")
    for x in sm["low"]:
        print(f"  {x['case_id']} 忠实{x['faithfulness']} 语气{x['tone']} 越权{x['overpromise']}：{x['verdict_zh']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
