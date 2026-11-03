"""售后邮箱（IMAP，企业微信邮箱）：读收件箱、处理完移到“已处理”、清理“已处理”里过期的邮件。"""
from __future__ import annotations

import base64
import imaplib
import re
from datetime import date

from ..config import get_settings


class MailboxError(RuntimeError):
    pass


def imap_utf7(name: str) -> str:
    """IMAP 文件夹名用的修改版 UTF-7（RFC 3501），中文文件夹名如“已处理”要这样编码。"""
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _q(name: str) -> str:
    return '"' + imap_utf7(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapMailbox:
    """with ImapMailbox() as box: for uid, raw in box.fetch_all(): ...; box.move_done(uid)"""

    def __init__(self):
        self.s = get_settings()
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self):
        try:
            self.conn = imaplib.IMAP4_SSL(self.s.aftersales_imap_host, self.s.aftersales_imap_port, timeout=60)
            self.conn.login(self.s.aftersales_mail_user, self.s.aftersales_mail_password)
        except imaplib.IMAP4.error:
            raise MailboxError("售后邮箱登录失败：检查 AFTERSALES_MAIL_USER、客户端专用密码，并确认邮箱已开启 IMAP") from None
        except OSError as e:
            raise MailboxError(f"连接售后邮箱失败：{type(e).__name__}") from None
        self._ensure(self.s.aftersales_mail_done_folder)
        return self

    def __exit__(self, *exc):
        if self.conn is not None:
            try:
                self.conn.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def _ok(self, typ, data, what: str):
        if typ != "OK":
            raise MailboxError(f"IMAP {what} 失败：{data!r}"[:300])
        return data

    def _ensure(self, folder: str) -> None:
        typ, _ = self.conn.select(_q(folder), readonly=True)
        if typ != "OK":
            self._ok(*self.conn.create(_q(folder)), f"创建文件夹 {folder}")

    def fetch_all(self, limit: int = 50) -> list[tuple[str, bytes]]:
        self._ok(*self.conn.select(_q(self.s.aftersales_mail_folder)), "打开收件箱")
        data = self._ok(*self.conn.uid("SEARCH", None, "ALL"), "搜索邮件")
        uids = (data[0] or b"").decode().split()[:limit]
        out = []
        for uid in uids:
            parts = self._ok(*self.conn.uid("FETCH", uid, "(BODY.PEEK[])"), "读取邮件")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append((uid, raw))
        return out

    def move_done(self, uid: str) -> None:
        """移到“已处理”（当前选中的是收件箱）。支持 MOVE 就用 MOVE，否则 复制 + 删除标记 + 清除。"""
        dest = _q(self.s.aftersales_mail_done_folder)
        caps = {c.decode().upper() if isinstance(c, bytes) else str(c).upper() for c in (self.conn.capabilities or ())}
        if "MOVE" in caps:
            self._ok(*self.conn.uid("MOVE", uid, dest), "移动邮件")
            return
        self._ok(*self.conn.uid("COPY", uid, dest), "复制邮件")
        self._ok(*self.conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), "标记删除")
        self._ok(*self.conn.expunge(), "清除邮件")

    def purge_done(self, before: date) -> int:
        """删除“已处理”里日期早于 before 的邮件（彻底删除），返回封数。"""
        self._ok(*self.conn.select(_q(self.s.aftersales_mail_done_folder)), "打开已处理文件夹")
        data = self._ok(*self.conn.uid("SEARCH", None, "BEFORE", _imap_date(before)), "搜索过期邮件")
        uids = (data[0] or b"").decode().split()
        for i in range(0, len(uids), 200):
            self._ok(*self.conn.uid("STORE", ",".join(uids[i:i + 200]), "+FLAGS", r"(\Deleted)"), "标记删除")
        if uids:
            self._ok(*self.conn.expunge(), "清除邮件")
        return len(uids)


def _imap_date(d: date) -> str:
    """IMAP 日期（英文月份，不受系统语言影响），如 01-Oct-2026。"""
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip(" .") or "_"
