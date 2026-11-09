"""一次性修复：以前售后邮箱把收件箱里所有邮件都收进系统、移到“已处理”（系统告警、谷仓通知等和售后无关的也收了）。

本脚本按新的判断规则（aftersales_mail.service.relevance）：
1. 把“已处理”里和售后无关的邮件移回收件箱（不删除、不改已读状态）；
2. 删掉系统里这些误收邮件的记录（“售后附件 → 待认领”里那些），以及它们带的附件文件（如谷仓查件进度 xls）。
和售后有关的邮件、记录不动。

先重启 main.py（新代码不会再把无关邮件移走），再运行：
    .venv\\Scripts\\python.exe scripts\\fix_misfiled_mails.py          # 只预览，不改任何东西
    .venv\\Scripts\\python.exe scripts\\fix_misfiled_mails.py --apply  # 确认后执行
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.aftersales_mail.mailbox import ImapMailbox, _q  # noqa: E402
from app.aftersales_mail.parse import parse  # noqa: E402
from app.aftersales_mail.service import _domain, relevance  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import AftersalesFile, AftersalesMail, AuditEvent  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（不加只预览）")
    args = ap.parse_args()

    irrelevant: set[str] = set()  # Message-ID
    relevant: set[str] = set()
    back_uids: list[str] = []  # 要移回收件箱的（有的系统告警邮件没有 Message-ID，所以按 UID 移）
    kinds: Counter = Counter()
    with ImapMailbox() as box:
        box._ok(*box.conn.select(_q(box.s.aftersales_mail_done_folder), readonly=True), "打开已处理文件夹")
        uids = (box._ok(*box.conn.uid("SEARCH", None, "ALL"), "搜索邮件")[0] or b"").decode().split()
        with session_scope() as db:
            for uid in uids:
                parts = box._ok(*box.conn.uid("FETCH", uid, "(BODY.PEEK[])"), "读取邮件")
                pm = parse(next((p[1] for p in parts if isinstance(p, tuple)), b""))
                mid = (pm.message_id or "").strip()
                ours = relevance(db, pm) is not None
                if mid:
                    (relevant if ours else irrelevant).add(mid)
                if not ours:
                    back_uids.append(uid)
                kinds[(_domain(pm.sender), "售后" if ours else "无关")] += 1
            db.rollback()
        print(f"“已处理”里共 {len(uids)} 封：售后 {len(uids) - len(back_uids)} 封（留着），无关 {len(back_uids)} 封（移回收件箱）")
        for (domain, kind), n in kinds.most_common():
            print(f"  {domain or '（无发件人）'} · {kind}：{n}")
        if args.apply:
            print("已移回收件箱：", box.move_back(back_uids), "封")

    root = get_settings().aftersales_root
    with session_scope() as db:
        recs = db.scalars(select(AftersalesMail)).all()
        # 误收：在“无关”名单里；或者既没认出订单、也没保存下任何文件（邮件已不在“已处理”里的旧告警记录）
        junk = [m for m in recs if m.message_id not in relevant
                and (m.message_id in irrelevant or (not m.order_id and not m.files))]
        files = [f for m in junk for f in db.scalars(select(AftersalesFile).where(AftersalesFile.mail_id == m.id))]
        print(f"系统里的邮件记录 {len(recs)} 条：误收 {len(junk)} 条（删除，附带文件 {len(files)} 个），保留 {len(recs) - len(junk)} 条")
        for m in recs:
            if m not in junk:
                print(f"  保留：#{m.id} {m.status} 订单 {m.order_id or '—'} 文件 {m.files}")
        if not args.apply:
            db.rollback()
            print("\n只是预览，什么都没改。确认无误后加 --apply 执行。")
            return
        for f in files:
            if f.rel_path:
                (root / f.rel_path).unlink(missing_ok=True)
            db.delete(f)
        for m in junk:
            db.delete(m)
        db.add(AuditEvent(actor="fix_misfiled_mails", action="AFTERSALES_MAIL_MISFILED_REMOVED",
                          detail={"mail_records": len(junk), "files": len(files), "moved_back": len(back_uids),
                                  "reason": "和售后无关的邮件被误收（系统告警、谷仓通知等）"}))
    print("完成。")


if __name__ == "__main__":
    main()
