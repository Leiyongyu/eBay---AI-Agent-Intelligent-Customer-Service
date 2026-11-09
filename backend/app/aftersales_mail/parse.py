"""解析售后邮件：主题、发件人、日期、正文、附件、WeTransfer 等文件分享链接、eBay 订单号。"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

# eBay 订单号（如 12-34567-89012）
ORDER_RE = re.compile(r"(?<![\d-])(\d{2}-\d{5}-\d{5})(?![\d-])")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
WETRANSFER_HOSTS = ("wetransfer.com", "we.tl")
# 其他大文件分享链接：系统不自动下载，登记下来提醒人工下载
OTHER_SHARE_HOSTS = ("mail.qq.com", "exmail.qq.com", "wx.mail.qq.com", "drive.google.com", "dropbox.com",
                     "icloud.com", "onedrive.live.com", "1drv.ms", "smash.gg", "fromsmash.com", "swisstransfer.com")


@dataclass
class Attachment:
    filename: str | None
    content_type: str
    data: bytes


@dataclass
class ParsedMail:
    message_id: str | None
    subject: str
    sender: str | None
    date: datetime | None
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    wetransfer_links: list[str] = field(default_factory=list)
    other_links: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)


def _html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw)
    hrefs = " ".join(re.findall(r'(?i)href=["\']([^"\']+)', raw))  # 链接常只在 href 里
    return html.unescape(re.sub(r"<[^>]+>", " ", raw)) + "\n" + hrefs


def _host(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url, re.I)
    return (m.group(1) if m else "").lower()


_WT_DOWNLOAD = re.compile(r"/downloads/([0-9a-f]+)", re.I)
_WT_SHORT = re.compile(r"^https?://we\.tl/t-[A-Za-z0-9]+", re.I)


def wetransfer_downloads(links: list[str]) -> list[str]:
    """只要真正的文件链接（wetransfer.com/downloads/… 或 we.tl/t-…），不要通知邮件里的首页、退订等链接；
    同一个传输出现多次（邮件里同一链接常有两种编码）只留一个；纯文本里被折行截断的半截链接
    （传输号是另一个链接传输号的前缀）也丢掉，不然会多出一条“下载失败”。"""
    found: list[tuple[str, str]] = []
    for u in links:
        if not _host_in(u, WETRANSFER_HOSTS):
            continue
        m = _WT_DOWNLOAD.search(u)
        key = m.group(1).lower() if m else (_WT_SHORT.match(u).group(0).lower() if _WT_SHORT.match(u) else None)
        if key and key not in {k for k, _ in found}:
            found.append((key, u))
    keys = [k for k, _ in found]
    return [u for k, u in found if not any(o != k and o.startswith(k) for o in keys)]


def _host_in(url: str, hosts: tuple[str, ...]) -> bool:
    h = _host(url)
    return any(h == x or h.endswith("." + x) for x in hosts)


def parse(raw: bytes) -> ParsedMail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    texts: list[str] = []
    attachments: list[Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        if filename or disposition == "attachment" or (disposition == "inline" and not ctype.startswith("text/")):
            data = part.get_payload(decode=True) or b""
            if data:
                attachments.append(Attachment(filename, ctype, data))
            continue
        if ctype == "text/plain":
            texts.append(part.get_content())
        elif ctype == "text/html":
            texts.append(_html_text(part.get_content()))
    subject = str(msg.get("Subject") or "")
    text = "\n".join(texts)
    links = list(dict.fromkeys(u.rstrip(".,;") for u in URL_RE.findall(text)))
    date = None
    try:
        if msg.get("Date"):
            date = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        date = None
    return ParsedMail(
        message_id=(str(msg.get("Message-ID") or "").strip() or None),
        subject=subject,
        sender=(parseaddr(str(msg.get("From") or ""))[1] or None),
        date=date,
        text=text,
        attachments=attachments,
        wetransfer_links=wetransfer_downloads(links),
        other_links=[u for u in links if _host_in(u, OTHER_SHARE_HOSTS)],
        order_ids=list(dict.fromkeys(ORDER_RE.findall(subject + "\n" + text))),
    )
