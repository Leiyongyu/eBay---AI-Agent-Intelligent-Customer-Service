"""WeTransfer 链接下载。WeTransfer 没有公开的下载接口，这里用它网页下载时用的接口：
短链 we.tl/t-xxx 先跳转到 wetransfer.com/downloads/<传输ID>/[<收件人ID>/]<安全码>，
再 POST /api/v4/transfers/<传输ID>/download 拿到直链。网站改版就可能失效——失败时登记链接，提醒人工下载。"""
from __future__ import annotations

import re
from typing import Iterator

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
DOWNLOAD_RE = re.compile(r"/downloads/([0-9a-f]+)/(?:([0-9a-f]+)/)?([0-9a-f]+)", re.I)


class WeTransferError(RuntimeError):
    pass


def resolve(url: str, client: httpx.Client) -> tuple[str, str, str | None]:
    """返回 (传输ID, 安全码, 收件人ID)。"""
    if not DOWNLOAD_RE.search(url):
        r = client.get(url, follow_redirects=True)
        url = str(r.url)
    m = DOWNLOAD_RE.search(url)
    if not m:
        raise WeTransferError("链接不是 WeTransfer 下载链接，或已失效")
    transfer_id, a, b = m.groups()
    return (transfer_id, b, a) if a else (transfer_id, b, None)


def direct_link(url: str, client: httpx.Client) -> str:
    transfer_id, security_hash, recipient_id = resolve(url, client)
    body = {"security_hash": security_hash, "intent": "entire_transfer"}
    if recipient_id:
        body["recipient_id"] = recipient_id
    r = client.post(f"https://wetransfer.com/api/v4/transfers/{transfer_id}/download", json=body,
                    headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
    if r.status_code != 200:
        raise WeTransferError(f"WeTransfer 返回 HTTP {r.status_code}（链接可能已过期，免费链接 7 天有效）")
    link = (r.json() or {}).get("direct_link")
    if not link:
        raise WeTransferError("WeTransfer 没有返回下载地址")
    return link


def open_download(url: str, client: httpx.Client) -> tuple[str | None, str, Iterator[bytes], httpx.Response]:
    """返回 (文件名, 类型, 数据块迭代器, 响应)；调用方读完后关闭响应。"""
    link = direct_link(url, client)
    resp = client.send(client.build_request("GET", link, headers={"User-Agent": UA}), stream=True,
                       follow_redirects=True)
    if resp.status_code != 200:
        resp.close()
        raise WeTransferError(f"下载文件返回 HTTP {resp.status_code}")
    name = None
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", cd, re.I)
    if m:
        from urllib.parse import unquote

        name = unquote(m.group(1) or m.group(2))
    if not name:
        name = link.split("?")[0].rsplit("/", 1)[-1] or "wetransfer.zip"
    ctype = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0]
    return name, ctype, resp.iter_bytes(1024 * 1024), resp
