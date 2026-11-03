"""eBay 消息正文的 HTML → 纯文本。

eBay 系统通知（FROM_EBAY，如退货退款、卖家等级变化）的 messageBody 是整封 HTML 邮件，动辄几万字，
绝大部分是样式。入库时转成纯文本放进 messages.body（工作台、列表预览、翻译、Agent 上下文都用它），
原文另存 messages.body_html，工作台可“查看原邮件”。只用标准库，不执行任何脚本、不取外部资源。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_DOC_HINT = re.compile(r"<!DOCTYPE\s+html|<html[\s>]|<body[\s>]", re.I)
_BLOCK_TAG = re.compile(r"<(?:div|table|tr|td|p|br|li|h[1-6])\b[^>]*>", re.I)
_SKIP = {"style", "script", "head", "title", "noscript", "template"}
_BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "section", "article", "header", "footer",
          "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "center"}
_SPACES = re.compile(r"[ \t\f\v ​‌‍﻿]+")


def looks_like_html(text: str | None) -> bool:
    """要有文档结构（<!DOCTYPE>/<html>/<body>）或至少 3 个块级标签才当 HTML；
    买家/店铺纯文本里偶尔出现的 <b>、单个 <br>、“< 5 EUR”之类不算，原样保留。"""
    if not text:
        return False
    head = text[:20000]
    return bool(_DOC_HINT.search(head)) or len(_BLOCK_TAG.findall(head, 0, 20000)) >= 3


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Collector()
    p.feed(html)
    p.close()
    lines = (_SPACES.sub(" ", line).strip() for line in "".join(p.parts).splitlines())
    return "\n".join(line for line in lines if line)


def split_body(body: str | None) -> tuple[str, str | None]:
    """返回（纯文本正文, 原始 HTML 或 None）。不是 HTML 时原样返回。"""
    body = body or ""
    if not looks_like_html(body):
        return body, None
    return html_to_text(body), body


def convert_stored(db, limit: int | None = None) -> int:
    """把库里还是 HTML 的消息转成纯文本、原文挪到 body_html（幂等：已有 body_html 的不再处理）。

    启动时执行一次，兜住升级前同步进来的消息；调用方负责提交。
    """
    from sqlalchemy import or_, select

    from ..models import Message

    pattern = or_(*(Message.body.ilike(p) for p in ("%<!doctype%", "%<html%", "%<body%", "%<div%", "%<table%",
                                                     "%<br%", "%<p>%", "%<p %", "%<span%", "%<td%")))
    q = select(Message).where(Message.body_html.is_(None), pattern).order_by(Message.id)
    if limit:
        q = q.limit(limit)
    n = 0
    for msg in db.scalars(q):
        text, html = split_body(msg.body)
        if html is None:
            continue
        msg.body, msg.body_html = text, html
        if msg.zh_translation:
            msg.zh_translation = None  # 旧译文是对 HTML 翻的，作废后按纯文本重新翻
        n += 1
    return n
