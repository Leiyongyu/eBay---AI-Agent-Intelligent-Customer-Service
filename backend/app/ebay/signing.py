"""eBay 数字签名（Digital Signatures for APIs）。

EU / UK 注册的卖家调用这些接口必须带签名（eBay 文档 “Digital Signatures for APIs”）：
Fulfillment issueRefund；Post-Order 的咨询 / 纠纷 / 退货退款、处理退货请求、创建 / 同意取消；Finances API 全部；
Trading GetAccount。别的接口、别的卖家带上签名 eBay 会忽略，所以配置好以后写操作一律带上。

签名按 RFC 9421（HTTP Message Signatures）+ RFC 9530（Content-Digest）：
  x-ebay-signature-key  Key Management API 给的“Public Key as JWE”
  Content-Digest        有请求体时：sha-256=:<base64(sha256(body))>:
  Signature-Input       sig1=("content-digest" "x-ebay-signature-key" "@method" "@path" "@authority");created=<unix 秒>
                        （没有请求体时去掉 "content-digest"）
  Signature             sig1=:<base64(用私钥对签名基串签名)>:
密钥支持 Ed25519（推荐）和 RSA（RSASSA-PKCS1-v1_5 + SHA-256）。私钥和 jwe 配在 .env（EBAY_SIGNING_PRIVATE_KEY、
EBAY_SIGNING_JWE），两项都填才签名；没配时返回空，eBay 若要求签名会报 215xxx，用 signature_error() 转成中文提示。
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from ..config import get_settings

SIGNATURE_CODES = re.compile(r"\b215[01]\d\d\b")


class SigningError(Exception):
    pass


def content_digest(body: bytes) -> str:
    """RFC 9530：sha-256=:<base64>:（eBay 只要 SHA-256）。"""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"


def _load_private_key(text: str):
    t = (text or "").strip()
    if not t:
        raise SigningError("没有配置签名私钥")
    try:
        if "BEGIN" in t:
            return serialization.load_pem_private_key(t.encode(), password=None)
        return serialization.load_der_private_key(base64.b64decode(re.sub(r"\s+", "", t)), password=None)
    except (ValueError, TypeError) as e:
        raise SigningError(f"签名私钥格式不对（要 PEM，或 base64 的 PKCS8）：{e}") from None


def signature_base(components: list[tuple[str, str]], params: str) -> str:
    """RFC 9421 §2.5 签名基串：每个组件一行 "名字": 值，最后一行 "@signature-params": 参数。"""
    return "\n".join(f'"{name}": {value}' for name, value in components) + f'\n"@signature-params": {params}'


def sign_headers(method: str, url: str, body: bytes | None, *, private_key: str | None = None, jwe: str | None = None,
                 created: int | None = None) -> dict[str, str]:
    """要加到请求上的签名头；没配置密钥返回 {}。body 必须是实际发出去的字节（json.dumps 之后的那份）。"""
    s = get_settings()
    private_key = s.ebay_signing_private_key if private_key is None else private_key
    jwe = s.ebay_signing_jwe if jwe is None else jwe
    if not (private_key or "").strip() or not (jwe or "").strip():
        return {}
    key = _load_private_key(private_key)
    jwe = jwe.strip()
    u = urlsplit(url)
    headers = {"x-ebay-signature-key": jwe}
    components: list[tuple[str, str]] = []
    if body:
        headers["Content-Digest"] = content_digest(body)
        components.append(("content-digest", headers["Content-Digest"]))
    components += [("x-ebay-signature-key", jwe), ("@method", method.upper()), ("@path", u.path or "/"),
                   ("@authority", u.netloc.lower())]
    params = "(" + " ".join(f'"{n}"' for n, _ in components) + f");created={int(created or time.time())}"
    base = signature_base(components, params).encode("ascii")
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(base)
    elif isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(base, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise SigningError("签名私钥只支持 Ed25519 或 RSA")
    headers["Signature-Input"] = f"sig1={params}"
    headers["Signature"] = "sig1=:" + base64.b64encode(sig).decode() + ":"
    return headers


def configured() -> bool:
    s = get_settings()
    return bool((s.ebay_signing_private_key or "").strip() and (s.ebay_signing_jwe or "").strip())


def signature_error_code(text: str | None) -> str | None:
    """eBay 返回里的数字签名错误码（215000–215199），没有返回 None。"""
    m = SIGNATURE_CODES.search(text or "")
    return m.group(0) if m else None


def signature_error(text: str | None) -> str | None:
    """eBay 返回的是数字签名相关的错误（215000–215199）时给一句中文提示，否则 None。"""
    if not text or not SIGNATURE_CODES.search(text):
        return None
    if not configured():
        return ("eBay 要求这个店铺的退款 / 取消请求带数字签名（EU/UK 注册卖家的规定），系统还没有配置签名密钥："
                "请管理员用 eBay Key Management API 生成签名密钥，填到 .env 的 EBAY_SIGNING_PRIVATE_KEY 和 EBAY_SIGNING_JWE")
    return "eBay 拒绝了数字签名（签名密钥可能不对或已失效，请管理员核对 EBAY_SIGNING_PRIVATE_KEY / EBAY_SIGNING_JWE）"
