"""AES-256-GCM 加解密（店铺凭证专用）。

- 主密钥只放在 .env 的 CREDENTIALS_MASTER_KEY（Base64 的 32 字节），不进数据库：只拿到数据库或备份文件解不开。
- 每条记录用随机 12 字节 nonce；AAD 绑定记录标识（store_key），密文挪到别的记录上会解密失败。
- 主密钥丢失则无法解密，只能用原始 Excel 重新覆盖导入。
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_VERSION = "k1"


class CredentialCryptoError(RuntimeError):
    pass


def new_master_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def _key(master_key_b64: str | None) -> bytes:
    if not master_key_b64:
        raise CredentialCryptoError("CREDENTIALS_MASTER_KEY 未配置，无法加解密店铺凭证")
    try:
        key = base64.b64decode(master_key_b64.strip(), validate=True)
    except ValueError:
        raise CredentialCryptoError("CREDENTIALS_MASTER_KEY 不是有效的 Base64") from None
    if len(key) != 32:
        raise CredentialCryptoError("CREDENTIALS_MASTER_KEY 必须是 32 字节（AES-256）")
    return key


def encrypt(master_key_b64: str, plaintext: bytes, aad: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key(master_key_b64)).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return base64.b64encode(nonce + ct).decode()


def decrypt(master_key_b64: str | None, token: str, aad: str) -> bytes:
    raw = base64.b64decode(token)
    try:
        return AESGCM(_key(master_key_b64)).decrypt(raw[:12], raw[12:], aad.encode("utf-8"))
    except InvalidTag:
        raise CredentialCryptoError("解密失败：主密钥不对，或密文与记录不匹配") from None


def fingerprint(secret: str) -> str:
    """不可逆的短指纹：用于比对“是不是同一个秘钥”，不能反推出秘钥。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
