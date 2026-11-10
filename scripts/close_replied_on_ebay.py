"""一次性检查：已被同事在 eBay 后台回复、却还挂在待处理里的任务（如 pieces_saleshop 任务 460）。

默认只预览（只读，不改库）：
    python scripts/close_replied_on_ebay.py
确认后才执行（关掉这些任务、作废草稿、原因 REPLIED_ON_EBAY；不给买家发任何消息、不动发件箱）：
    python scripts/close_replied_on_ebay.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from app import ebay_replied
    from app.db import session_scope
    from app.models import Message, ReplyJob, Store

    ap = argparse.ArgumentParser(description="已在 eBay 后台回复、还挂在待处理的任务（默认只预览）")
    ap.add_argument("--apply", action="store_true", help="确认执行：关掉列出的任务")
    args = ap.parse_args(argv)
    with session_scope() as db:
        names = {s.id: s.name for s in db.query(Store)}
        rows = ebay_replied.stale_pending(db)
        print(f"共 {len(rows)} 个任务：已有晚于它最后一条买家消息的店铺后台回复，还没关")
        for r in rows:
            print(f"  任务 #{r['job_id']}（{r['state']}）{names.get(r['store_id'], r['store_id'])} 会话 {r['conversation_id']}："
                  f"买家 {r['buyer_at']:%m-%d %H:%M} → 店铺后台回复 {r['seller_at']:%m-%d %H:%M}"
                  f"「{r['seller_preview'].replace(chr(10), ' ')[:50]}」")
        if not args.apply:
            print("这是预览，没有改库；确认后加 --apply 执行。")
            db.rollback()
            return 0
        for r in rows:
            job = db.get(ReplyJob, r["job_id"])
            reply = db.query(Message).filter(Message.conversation_id == r["conversation_id"],
                                             Message.ebay_message_id == r["seller_message_id"]).one()
            ebay_replied.close_job(db, job, reply, actor="close_replied_on_ebay")
        print(f"已关掉 {len(rows)} 个任务（没有给买家发任何消息）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
