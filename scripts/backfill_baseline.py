"""补拉上线前业务基线（开发方案第 2.1 节）：从 eBay 只读补拉对接店铺上线前的买家会话，存进 baseline_* 三张表，只用于统计。

只调 eBay Message API 的两个只读 GET；不调模型、不碰飞书 / 17TRACK / 谷仓 / 邮件；不写工作台的会话和消息表、不建回复任务。

默认试运行（只列会话、抽样取几个会话估算消息数，不写库）：
    python scripts/backfill_baseline.py
确认后写库（预计接口调用超过 --max-calls，默认 2000，会先停下）：
    python scripts/backfill_baseline.py --apply
中断后接着拉（跳过这个批次已写好的会话）：
    python scripts/backfill_baseline.py --apply --resume 3
参数：--stores pieces_saleshop,Automodell-Block（默认 EBAY_LIVE_STORES）、--start 2026-09-10、--end 2026-11-04（含当天）、
     --pause 0.2（每次调用前停顿秒数）、--sample 10（试运行抽样会话数）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def print_report(r: dict) -> None:
    if r.get("error") and not r.get("stores"):
        print("❌ " + r["error"])
        return
    print(f"时间段：{r['period_start']} 到 {r['period_end']}（不含）")
    for sid, st in r["stores"].items():
        probe = st.get("probe") or {}
        print(f"\n【{sid}】" + (f" ❌ {st['error']}" if st.get("error") else ""))
        if probe.get("error"):
            print(f"  探测最早时间失败：{probe['error']}")
        elif probe:
            print(f"  eBay 上这个店铺共 {probe.get('total_conversations')} 个买家会话，最早能取到 {probe.get('earliest_available')}")
        print(f"  时间段起点之后有更新的会话 {st['listed']} 个：时间段内可能有消息的 {st['candidates']} 个"
              f"（上线后才开始的 {st['skipped_after_period']} 个、更早的 {st['skipped_before_period']} 个不取）"
              + (f"，待取 {st['todo']} 个" if st['todo'] != st['candidates'] else ""))
        if st.get("list_truncated"):
            print("  ⚠ 会话列表翻到了页数上限，可能没列全")
        if "sample" in st:
            sm = st["sample"]
            print(f"  抽样 {sm.get('conversations', 0)} 个会话：时间段内消息 {sm.get('messages', 0)} 条（买家 {sm.get('buyer', 0)}、"
                  f"店铺 {sm.get('seller', 0)}），没有时间段内消息的 {sm.get('empty', 0)} 个；估算全部约 {st.get('estimated_messages', 0)} 条")
        if st.get("sample_error"):
            print(f"  抽样出错：{st['sample_error']}")
        if "written_conversations" in st:
            print(f"  写入会话 {st['written_conversations']} 个、新消息 {st['written_messages']} 条；"
                  f"时间段内没消息的 {st['empty']} 个；出错 {st['errors']} 个；接口调用 {st['api_calls']} 次")
    print()
    if not r.get("apply"):
        print(f"试运行，没有写库。本次接口调用 {r['api_calls_used']} 次（列会话 {r['list_calls']}、抽样 {r['sample_calls']}）。")
        print(f"写库预计接口调用约 {r['estimated_calls']} 次，估算消息约 {r['estimated_messages']} 条。确认后加 --apply。")
        return
    if r.get("stopped") == "OVER_LIMIT":
        print("⛔ " + r["error"])
        return
    print(f"批次 #{r['batch_id']}：{r['status']}。本次写入会话 {r['conversations']} 个、消息 {r['messages']} 条"
          f"（买家 {r['messages_buyer']}、店铺 {r['messages_seller']}）；本次接口调用 {r['api_calls_used']} 次。")
    print(f"本批次累计：会话 {r['batch_conversations_total']} 个、消息 {r['batch_messages_total']} 条，"
          f"消息时间 {r['first_message_at']} 到 {r['last_message_at']}")
    if r.get("errors"):
        print("出错：\n  " + "\n  ".join(r["errors"][-10:]))
        print(f"可以用 --apply --resume {r['batch_id']} 接着拉。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="补拉上线前业务基线（默认试运行，不写库）")
    ap.add_argument("--apply", action="store_true", help="写库（eBay 仍然只读）")
    ap.add_argument("--resume", type=int, help="接着拉这个批次（沿用它的店铺和时间段）")
    ap.add_argument("--stores", help="店铺 ID / eBay 账号，逗号分隔；默认 EBAY_LIVE_STORES")
    ap.add_argument("--start", default="2026-09-10", help="起始日期（含，UTC）")
    ap.add_argument("--end", default="2026-11-04", help="结束日期（含当天，UTC）")
    ap.add_argument("--max-calls", type=int, default=2000, help="写库时预计接口调用超过这个数就先停下")
    ap.add_argument("--pause", type=float, default=0.2, help="每次调用 eBay 前停顿的秒数（限速）")
    ap.add_argument("--sample", type=int, default=10, help="试运行时每个店铺抽样取多少个会话估算消息数")
    ap.add_argument("--json", action="store_true", help="另外打印完整结果 JSON（不含消息正文）")
    args = ap.parse_args(argv)

    from app import runtime_settings
    from app.baseline import BaselineBackfill
    from app.db import init_db
    from app.ebay.stores import ebay_not_ready_reasons

    if args.apply:
        init_db()  # 建 baseline_* 表（已有则不动）
    runtime_settings.load()  # 系统配置页改过的项优先于 .env（只读）
    reasons = ebay_not_ready_reasons()
    if reasons:
        print("❌ eBay 配置不完整：" + "；".join(reasons))
        return 1
    keys = {x.strip().lower() for x in (args.stores or "").replace("，", ",").split(",") if x.strip()} or None
    result = BaselineBackfill(pause=args.pause, sample=args.sample, echo=lambda m: print(m, flush=True)).run(
        store_keys=keys, start=_day(args.start), end=_day(args.end) + timedelta(days=1), apply=args.apply,
        resume_batch=args.resume, max_calls=args.max_calls)
    print_report(result)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
