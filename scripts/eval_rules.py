"""第一层规则评测（mock，不调模型、不连外部系统）：在独立的 SQLite 评测库里回放黄金用例，跑当前的整条起草流水线，
按意图出通过率，结果记进业务库的 eval_runs（版本门槛、系统状态页、上线效果页都读它）。

    python scripts/eval_rules.py                      # 用 data/eval/golden.jsonl（没有就用 tests/fixtures/golden_sample.jsonl）
    python scripts/eval_rules.py --sample             # 只跑 CI 用的 10 条样例
    python scripts/eval_rules.py --gate               # 低于门槛（EVAL_MIN_PASS_RATE）或比基线掉太多时返回 1（CI 用）
    python scripts/eval_rules.py --set-baseline       # 这次结果（没低于门槛时）标为基线
    python scripts/eval_rules.py --no-record          # 不写 eval_runs（CI 里没有业务库时用）
    python scripts/eval_rules.py --live --cases G-xx  # 真实调用模型重新起草后再断言（要用户同意；会先估算并要求输入 yes）

报告：data/eval/runs/<时间>/report.json、report.md。
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


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def write_report(rep: dict, meta: dict, out_dir: Path) -> Path:
    s = rep["summary"]
    lines = [f"# 第一层规则评测 {meta['at']}", "",
             f"- 数据：{meta['dataset']}（{s['cases']} 条，评了 {s['judged']} 条，跳过 {s['skipped']} 条）",
             f"- 通过率：**{_pct(s['pass_rate'])}**（有当时模型输出 {_pct(s['by_mode'].get('recorded'))}，"
             f"人工回复替身 {_pct(s['by_mode'].get('standin'))}）",
             f"- 版本：{json.dumps(meta['versions'], ensure_ascii=False)}",
             f"- 用例来源：{json.dumps(s['dataset']['source'], ensure_ascii=False)}；标签来源："
             f"{json.dumps(s['dataset']['label_source'], ensure_ascii=False)}（auto = 规则自动标、待人工复核）", "",
             "## 按来源 / 标签来源", "", "| 分组 | 条数 | 通过率 |", "|---|---|---|",
             *[f"| 来源 {k} | {v['n']} | {_pct(v['pass_rate'])} |" for k, v in s["by_source"].items()],
             *[f"| 标签 {k} | {v['n']} | {_pct(v['pass_rate'])} |" for k, v in s["by_label_source"].items()],
             *[f"| 当时提示词 {k} | {v['n']} | {_pct(v['pass_rate'])} |" for k, v in s["by_prompt_version"].items()],
             "", "## 按检查项", "",
             "| 检查 | 评了 | 通过率 |", "|---|---|---|"]
    lines += [f"| {k} | {v['n']} | {_pct(v['pass_rate'])} |" for k, v in s["by_check"].items()]
    lines += ["", "## 按意图", "", "| 意图 | 条数 | 通过率 |", "|---|---|---|"]
    lines += [f"| {k} | {v['n']} | {_pct(v['pass_rate'])} |" for k, v in s["by_stratum"].items()]
    if s["failures"]:
        lines += ["", "## 没通过的用例（前 50）", "", "| 用例 | 意图 | 没过的检查 / 错误 |", "|---|---|---|"]
        lines += [f"| {f['case_id']} | {f['stratum']} | {f['error'] or '、'.join(f['failed'])} |" for f in s["failures"]]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps({**rep, "meta": meta}, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    p = out_dir / "report.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=None)
    ap.add_argument("--sample", action="store_true", help="只跑 tests/fixtures/golden_sample.jsonl")
    ap.add_argument("--cases", default=None, help="逗号分隔的 case_id，只跑这些")
    ap.add_argument("--trigger", default="manual", choices=("manual", "startup", "ci", "nightly"))
    ap.add_argument("--gate", action="store_true", help="不过时返回 1")
    ap.add_argument("--set-baseline", action="store_true")
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--live", action="store_true", help="真实调用模型重新起草（要用户同意）")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out-dir", default=None, help="报告和评测库放哪（默认 data/eval/runs/<时间>）")
    a = ap.parse_args(argv)

    from app.evaluation import gate, golden, rules_eval
    from app.evaluation.isolate import isolated_db

    path = Path(a.file) if a.file else (golden.SAMPLE_FILE if a.sample or not golden.GOLDEN_FILE.exists() else golden.GOLDEN_FILE)
    dataset = "sample" if path == golden.SAMPLE_FILE else "golden"
    cases = golden.load(path)
    if a.cases:
        want = {x.strip() for x in a.cases.split(",")}
        cases = [c for c in cases if c["case_id"] in want]
    if not cases:
        print(f"没有用例：{path}")
        return 1
    if a.live:
        n = len(cases)
        print(f"--live 会真实调用模型起草 {n} 条（约 {n * 2} 次调用、{n * 15000} 输入 token），需要用户同意。")
        if not a.yes and input("输入 yes 继续：").strip().lower() != "yes":
            print("已取消")
            return 1

    versions = None
    if not a.no_record:
        from app.db import session_scope

        with session_scope() as db:  # 版本取业务库的（知识库指纹要看正式的知识）
            versions = gate.current_versions(db)
    at = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(a.out_dir) if a.out_dir else golden.ROOT / "data" / "eval" / "runs" / at
    error = None
    rep = {"summary": {}, "rows": []}
    try:
        with isolated_db(out_dir):
            rep = rules_eval.run(cases, live=a.live)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {str(e)[:500]}"
    meta = {"at": at, "dataset": f"{dataset}（{path.name}）", "versions": versions, "trigger": a.trigger, "live": a.live}
    report = write_report(rep, meta, out_dir) if rep.get("summary") else None
    run = None
    if not a.no_record:
        from app.db import session_scope

        with session_scope() as db:
            run = gate.record(db, rep.get("summary") or {}, trigger=a.trigger, dataset=dataset, versions=versions,
                              error=error, set_baseline=a.set_baseline)
            run_id, below = run.id, run.below
    else:
        below = bool(error) or gate.judge((rep.get("summary") or {}).get("pass_rate"), None)
    s = rep.get("summary") or {}
    if not a.quiet:
        if error:
            print("评测出错：", error)
        else:
            print(f"通过率 {_pct(s['pass_rate'])}（{s['judged']}/{s['cases']} 条评了）；按检查项：",
                  {k: _pct(v["pass_rate"]) for k, v in s["by_check"].items()})
            for k, v in s["by_stratum"].items():
                print(f"  {k:<20} {v['n']:>4} 条  {_pct(v['pass_rate'])}")
            print("报告：", report)
        if run is not None:
            print(f"已记录 eval_runs #{run_id}{'（低于门槛，状态页标红）' if below else ''}"
                  f"{'，标为基线' if a.set_baseline and not below else ''}")
    return 1 if a.gate and below else 0


if __name__ == "__main__":
    raise SystemExit(main())
