"""上线前清空：删除本系统里全部买家会话和消息（含图片、回复任务、草稿、发件箱、飞书工单、发票申请），
并重置对接店铺的消息拉取进度——重启 main.py 后按 EBAY_SYNC_LOOKBACK_DAYS 从 eBay 重新拉取真实消息。

不删：谷仓查件单、发票留底、售后附件与邮件、发货通知、eBay 售后数据（纠纷 / 退货）、订单汇总、异常包裹跟进表
（这些只解除和会话的关联）。只删本系统的数据，不动 eBay 上的任何东西。

    .venv\\Scripts\\python.exe scripts\\purge_all_conversations.py          # 只预览数量
    .venv\\Scripts\\python.exe scripts\\purge_all_conversations.py --apply  # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import delete, func, select, update  # noqa: E402

from app.db import session_scope  # noqa: E402
from app.ebay.stores import ebay_store_targets  # noqa: E402
from app.models import (  # noqa: E402
    AftersalesFile,
    AftersalesMail,
    AuditEvent,
    Conversation,
    EbaySyncState,
    Message,
    ParcelFollowup,
)
from app.retention import _delete_files, delete_conversations  # noqa: E402

BATCH = 200


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（不加只预览）")
    args = ap.parse_args()
    targets = ebay_store_targets()
    with session_scope() as db:
        n_conv = db.scalar(select(func.count()).select_from(Conversation)) or 0
        n_msg = db.scalar(select(func.count()).select_from(Message)) or 0
    print(f"本系统现有会话 {n_conv} 个、消息 {n_msg} 条，将全部删除；")
    print("之后重新拉取的店铺：", "、".join(t.name or t.store_id for t in targets))
    if not args.apply:
        print("\n只是预览，什么都没改。确认后加 --apply 执行。")
        return
    total = {"conversations": 0, "messages": 0, "attachments": 0, "jobs": 0}
    files: set[str] = set()
    while True:
        with session_scope() as db:
            ids = list(db.scalars(select(Conversation.id).order_by(Conversation.id).limit(BATCH)))
            if not ids:
                break
            for model in (AftersalesMail, AftersalesFile, ParcelFollowup):
                db.execute(update(model).where(model.conversation_id.in_(ids)).values(conversation_id=None))
            counts, f = delete_conversations(db, ids)
            files |= f
            for k in total:
                total[k] += counts.get(k, 0)
    removed_files = _delete_files(files)
    with session_scope() as db:
        reset = db.execute(delete(EbaySyncState)).rowcount or 0  # 拉取进度清掉：重启后按回看天数重新拉
        db.add(AuditEvent(actor="purge_all_conversations", action="ALL_CONVERSATIONS_PURGED",
                          detail={**total, "files": removed_files, "sync_state_reset": reset,
                                  "sync_reset_stores": [t.name or t.store_id for t in targets],
                                  "kept": "查件单、发票留底、售后附件、发货通知、售后数据、订单汇总、异常包裹跟进（断开会话关联）"}))
    print(f"已删除：会话 {total['conversations']}、消息 {total['messages']}、图片记录 {total['attachments']}（文件 {removed_files}）、"
          f"回复任务 {total['jobs']}；拉取进度重置 {reset} 条。重启 main.py 后重新拉取。")


if __name__ == "__main__":
    main()
