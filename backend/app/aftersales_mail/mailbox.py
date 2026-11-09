"""售后邮箱（IMAP，企业微信邮箱）：只读收件箱里新来的邮件（按 UID 进度）、和售后有关的处理完移到“已处理”、
清理“已处理”里本系统处理过的过期邮件。售后邮箱可能就是同事自己的工作邮箱：无关邮件一律不移动、不删除、不改已读状态。"""
from __future__ import annotations

import base64
import imaplib
import re
from datetime import date
from email.parser import BytesHeaderParser

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

    def fetch_new(self, after_uid: int | None, uidvalidity: str | None, since: date,
                  limit: int = 50) -> tuple[str | None, list[tuple[str, bytes]]]:
        """收件箱里 UID 大于 after_uid 的邮件（旧到新，最多 limit 封）；第一次或 UIDVALIDITY 变了，按日期从 since 开始。
        用 BODY.PEEK 读，不改已读状态。返回（当前 UIDVALIDITY, [(uid, 原文)]）。"""
        self._ok(*self.conn.select(_q(self.s.aftersales_mail_folder)), "打开收件箱")
        validity = next((v.decode() if isinstance(v, bytes) else str(v)
                         for v in (self.conn.response("UIDVALIDITY")[1] or []) if v), None)
        if after_uid is None or (uidvalidity and validity and uidvalidity != validity):
            data = self._ok(*self.conn.uid("SEARCH", None, "SINCE", _imap_date(since)), "搜索邮件")
            uids = sorted(int(u) for u in (data[0] or b"").decode().split())
        else:
            data = self._ok(*self.conn.uid("SEARCH", None, "UID", f"{after_uid + 1}:*"), "搜索邮件")
            uids = sorted(u for u in (int(x) for x in (data[0] or b"").decode().split()) if u > after_uid)
        out = []
        for uid in uids[:limit]:
            parts = self._ok(*self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])"), "读取邮件")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            out.append((str(uid), raw or b""))
        return validity, out

    def _message_ids(self, uids: list[str]) -> dict[str, str]:
        """{uid: Message-ID}（只读邮件头）。"""
        out = {}
        for i in range(0, len(uids), 100):
            parts = self._ok(*self.conn.uid("FETCH", ",".join(uids[i:i + 100]), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"),
                             "读取邮件头")
            for p in parts:
                if isinstance(p, tuple):
                    m = re.search(rb"UID (\d+)", p[0])
                    mid = str(BytesHeaderParser().parsebytes(p[1]).get("Message-ID") or "").strip()
                    if m and mid:
                        out[m.group(1).decode()] = mid
        return out

    def move_back(self, uids: list[str]) -> int:
        """把“已处理”里这些 UID 的邮件移回收件箱（纠正以前把无关邮件也移走的问题），返回封数。"""
        self._ok(*self.conn.select(_q(self.s.aftersales_mail_done_folder)), "打开已处理文件夹")
        dest = _q(self.s.aftersales_mail_folder)
        for uid in uids:
            self.move_done(uid, dest)
        return len(uids)

    def move_done(self, uid: str, dest: str | None = None) -> None:
        """移到“已处理”（当前选中的是收件箱）。支持 MOVE 就用 MOVE，否则 复制 + 删除标记 + 清除。"""
        dest = dest or _q(self.s.aftersales_mail_done_folder)
        caps = {c.decode().upper() if isinstance(c, bytes) else str(c).upper() for c in (self.conn.capabilities or ())}
        if "MOVE" in caps:
            self._ok(*self.conn.uid("MOVE", uid, dest), "移动邮件")
            return
        self._ok(*self.conn.uid("COPY", uid, dest), "复制邮件")
        self._ok(*self.conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), "标记删除")
        self._ok(*self.conn.expunge(), "清除邮件")

    def purge_done(self, before: date, message_ids: set[str]) -> int:
        """删除“已处理”里日期早于 before、而且是本系统处理过的（Message-ID 在 message_ids 里）邮件（彻底删除），返回封数。
        别人自己放进“已处理”的邮件、以前误移进来的无关邮件都不删。"""
        self._ok(*self.conn.select(_q(self.s.aftersales_mail_done_folder)), "打开已处理文件夹")
        data = self._ok(*self.conn.uid("SEARCH", None, "BEFORE", _imap_date(before)), "搜索过期邮件")
        uids = [u for u, mid in self._message_ids((data[0] or b"").decode().split()).items() if mid in message_ids]
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
