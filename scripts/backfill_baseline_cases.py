"""补拉上线前基线期新开的 eBay 售后（咨询 / 纠纷 / 退货 / 付款纠纷）进 ebay_cases（开发方案第 2.1 节“沟通后升级率”）。

只调 eBay 只读查询接口（Post-Order return / inquiry / casemanagement search、Fulfillment payment_dispute_summary），
按创建时间查；不调模型、不碰飞书 / 17TRACK / 谷仓 / 邮件。只插入 ebay_cases 里还没有的单，已有的一律不动。

默认试运行（查一遍、只统计，不写库）：
    python scripts/backfill_baseline_cases.py
确认后写库：
    python scripts/backfill_baseline_cases.py --apply
参数：--stores（默认 EBAY_LIVE_STORES；--all-stores 全部有秘钥的店铺）、--start 2026-09-10、--end 2026-11-04（含当天，UTC）、
     --days 90（最近 N 天，代替 --start / --end）、--origin（写进 detail.origin 的来源，默认 BASELINE_BACKFILL）、
     --case-details（时间段内还不知道类型的纠纷逐张查详情补 caseType）
售后原因分类用（2026-11-10）：全部店铺最近 90 天
    python scripts/backfill_baseline_cases.py --all-stores --days 90 --origin CASE_BACKFILL_90D --case-details
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LABELS = {"INQUIRY": "咨询", "CASE": "纠纷", "RETURN": "退货", "PAYMENT_DISPUTE": "付款纠纷"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="补拉基线期 eBay 售后（默认试运行，不写库）")
    ap.add_argument("--apply", action="store_true", help="写库（只插入还没有的单；eBay 仍然只读）")
    ap.add_argument("--stores", help="店铺 ID / eBay 账号，逗号分隔；默认 EBAY_LIVE_STORES")
    ap.add_argument("--start", default="2026-09-10", help="起始日期（含，UTC）")
    ap.add_argument("--end", default="2026-11-04", help="结束日期（含当天，UTC）")
    ap.add_argument("--all-stores", action="store_true", help="全部有秘钥的店铺")
    ap.add_argument("--days", type=int, help="最近 N 天（代替 --start / --end）")
    ap.add_argument("--origin", default="BASELINE_BACKFILL", help="来源标记（detail.origin）")
    ap.add_argument("--case-details", action="store_true", help="还不知道类型的纠纷查详情补 caseType")
    ap.add_argument("--json", action="store_true", help="另外打印完整结果 JSON")
    args = ap.parse_args(argv)

    from app import runtime_settings
    from app.case_backfill import CaseBackfill
    from app.ebay.stores import ebay_not_ready_reasons

    runtime_settings.load()
    reasons = ebay_not_ready_reasons()
    if reasons:
        print("❌ eBay 配置不完整：" + "；".join(reasons))
        return 1
    keys = {"*"} if args.all_stores else \
        ({x.strip().lower() for x in (args.stores or "").replace("，", ",").split(",") if x.strip()} or None)
    if args.days:
        end = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        start = end - timedelta(days=args.days)
    else:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d") + timedelta(days=1)
    r = CaseBackfill().run(store_keys=keys, start=start, end=end, apply=args.apply, origin=args.origin,
                           case_details=args.case_details)
    if r.get("error"):
        print("❌ " + r["error"])
        return 1
    print(f"时间段：{r['period_start']} 到 {r['period_end']}（不含），{'写库' if r['apply'] else '试运行，不写库'}")
    for sid, st in r["stores"].items():
        print(f"\n【{st['name']}】（{sid}）接口调用 {st['api_calls']} 次")
        for kind, label in LABELS.items():
            k, info = st["kinds"].get(kind), st["info"].get(kind) or {}
            if info.get("error"):
                print(f"  {label}：❌ {info['error']}")
                continue
            extra = f"；offset 含义：{info['offset_mode']}" if info.get("offset_mode") else ""
            after = f" → 补拉后 {st['after'][kind]}" if st.get("after") else ""
            print(f"  {label}：eBay 上时间段内 {k['found']} 张，库里已有 {k['already']}，新增 {k['new']}（其中未结 {k['new_open']}）；"
                  f"库里时间段内原有 {st['before'][kind]}{after}{extra}")
    for sid, st in r["stores"].items():
        if st.get("case_types") or st.get("old_cases_without_type"):
            print(f"  {st['name']} 纠纷类型：{st.get('case_types') or {}}，库里原有不知类型 {st.get('old_cases_without_type', 0)} 张")
    tot = {k: sum(st["kinds"].get(k, {}).get("new", 0) for st in r["stores"].values()) for k in LABELS}
    print("\n合计新增：" + "，".join(f"{LABELS[k]} {v}" for k, v in tot.items()))
    print(f"共调用 eBay 接口 {r['api_calls']} 次（其中纠纷详情 {r['case_detail_calls']} 次）。" +
          ("" if r["apply"] else f"写库预计约 {r['estimated_apply_calls']} 次（含纠纷详情 {r['case_details_needed']} 张）。确认后加 --apply 写库。"))
    if args.json:
        print(json.dumps(r, ensure_ascii=False, default=str))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
