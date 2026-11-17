"""店铺凭证（紫鸟店铺全量表）：加密入库、整表覆盖导入、按需解密。

约束（用户要求）：
- 加密存储：店铺账号、店铺 IP、IP 是否过期、browserId、秘钥合成一个 JSON 整体加密；店铺名称、平台、站点 ID 明文，便于显示和匹配。
- 只能覆盖、不能修改：唯一的写入口是 import_credentials()，在一个事务里删掉旧记录、写入新文件的全部记录；
  数据库触发器禁止 UPDATE（PostgreSQL 上 DELETE 也只允许在导入事务里执行，见 db.install_credential_guards）。
- 不能导出：没有任何接口返回明文或密文；list_masked() 只返回名称、平台、是否有秘钥和不可逆指纹。
  明文只在进程内按需解密（如调用 eBay 接口时取 refresh token），不写日志。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import utcnow
from ..models import Store, StoreCredential, StoreCredentialImport
from .crypto import KEY_VERSION, decrypt, encrypt, fingerprint

HEADERS = {"序号": "seq", "店铺名称": "store_name", "平台": "platform", "店铺账号": "account", "店铺IP": "ip",
           "IP已过期": "ip_expired", "browserId": "browser_id", "站点ID": "site_id", "秘钥": "secret"}
REQUIRED = ("店铺名称", "平台", "秘钥")


class CredentialImportError(ValueError):
    pass


@dataclass(frozen=True)
class DecryptedCredential:
    store_key: str
    store_name: str
    platform: str
    site_id: str | None
    account: str | None
    ip: str | None
    ip_expired: str | None
    browser_id: str | None
    secret: str | None = field(repr=False)  # 不出现在 repr / 日志里

    def __repr__(self) -> str:
        return f"DecryptedCredential({self.store_key}, {self.store_name}, {self.platform}, secret=***)"


def make_store_key(store_name: str, platform: str) -> str:
    return "ZN-" + hashlib.sha256(f"{store_name.strip()}|{platform.strip()}".encode("utf-8")).hexdigest()[:12]


def _aad(store_key: str) -> str:
    return f"store_credentials:{store_key}:{KEY_VERSION}"


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def read_excel(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise CredentialImportError("文件是空的")
    header = [_clean(h) for h in rows[0]]
    missing = [h for h in REQUIRED if h not in header]
    if missing:
        raise CredentialImportError(f"缺少表头：{'、'.join(missing)}（第一行须包含 {'、'.join(HEADERS)}）")
    idx = {HEADERS[h]: i for i, h in enumerate(header) if h in HEADERS}
    out = []
    for n, r in enumerate(rows[1:], 2):
        rec = {k: _clean(r[i]) if i < len(r) else None for k, i in idx.items()}
        if not rec.get("store_name") and not rec.get("platform"):
            continue  # 空行
        if not rec.get("store_name") or not rec.get("platform"):
            raise CredentialImportError(f"第 {n} 行缺少店铺名称或平台")
        rec["seq"] = int(rec["seq"]) if rec.get("seq") and str(rec["seq"]).isdigit() else None
        out.append(rec)
    keys = [make_store_key(r["store_name"], r["platform"]) for r in out]
    dup = {k for k in keys if keys.count(k) > 1}
    if dup:
        names = sorted({r["store_name"] for r in out if make_store_key(r["store_name"], r["platform"]) in dup})
        raise CredentialImportError(f"店铺名称 + 平台重复：{'、'.join(names)}")
    return out


MARKETPLACES = {"eBay-德国": ("EBAY_DE", "Europe/Berlin"), "eBay-英国": ("EBAY_GB", "Europe/London"),
                "eBay-美国": ("EBAY_US", "America/Los_Angeles"), "eBay-香港": ("EBAY_HK", "Asia/Hong_Kong")}


def _system_store_id(store_key: str, secret: str | None) -> str:
    """.env 现用 refresh token 对应的店铺沿用 EBAY_STORE_ID（已同步的会话不用迁移），其余用 store_key。"""
    s = get_settings()
    if secret and s.ebay_refresh_token and fingerprint(secret) == fingerprint(s.ebay_refresh_token.strip()):
        return s.ebay_store_id
    return store_key


def import_credentials(db: Session, path: Path, actor: str, profile_fetcher=None, progress=None) -> dict:
    """整表覆盖：同一事务内删除全部旧记录并写入新文件；任何一行出错则整体不生效。

    带秘钥的 eBay 店铺会先用该秘钥只读查询 eBay 卖家账号与原始店铺名（GetUser / GetStore），一并写入；
    查询失败只记原因，不影响导入。随后为这些店铺建立/更新系统店铺（stores），供工作台店铺筛选。
    """
    master = get_settings().credentials_master_key
    records = read_excel(path)
    if profile_fetcher is None:
        from ..ebay.profile import fetch_seller_profile as profile_fetcher
    profiles = {}
    ebay_rows = [r for r in records if r.get("secret") and r["platform"] in MARKETPLACES]
    for i, r in enumerate(ebay_rows, 1):  # 网络查询放在数据库事务之外
        profiles[make_store_key(r["store_name"], r["platform"])] = profile_fetcher(r["secret"])
        if progress:
            progress(i, len(ebay_rows), r["store_name"])
    fetched_at = utcnow()
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SET LOCAL app.credentials_import = 'on'"))  # 触发器只在导入事务里放行 DELETE
    replaced = db.scalar(select(func.count()).select_from(StoreCredential)) or 0
    if dialect == "mysql":  # MySQL 没有事务级变量：会话变量只在删除这一句前后打开，删完马上清掉（连接会被连接池复用）
        db.execute(text("SET @credentials_import = 'on'"))
        try:
            db.execute(delete(StoreCredential))
        finally:
            db.execute(text("SET @credentials_import = NULL"))
    else:
        db.execute(delete(StoreCredential))
    batch = StoreCredentialImport(file_name=path.name, file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  rows=len(records), rows_with_secret=sum(bool(r.get("secret")) for r in records),
                                  replaced_rows=replaced, key_version=KEY_VERSION, imported_by=actor[:64])
    db.add(batch)
    db.flush()
    for r in records:
        key = make_store_key(r["store_name"], r["platform"])
        payload = json.dumps({k: r.get(k) for k in ("account", "ip", "ip_expired", "browser_id", "secret")},
                             ensure_ascii=False).encode("utf-8")
        p = profiles.get(key)
        db.add(StoreCredential(
            store_key=key, seq=r.get("seq"), store_name=r["store_name"][:200], platform=r["platform"][:64],
            site_id=r.get("site_id"), has_secret=bool(r.get("secret")),
            secret_fingerprint=fingerprint(r["secret"]) if r.get("secret") else None,
            encrypted_payload=encrypt(master, payload, _aad(key)), key_version=KEY_VERSION, import_id=batch.id,
            store_id=_system_store_id(key, r.get("secret")) if p and p.user_id else None,
            ebay_user_id=p.user_id if p else None, ebay_store_name=p.store_name if p else None,
            ebay_store_url=p.store_url if p else None, ebay_registration_site=p.registration_site if p else None,
            ebay_profile_error=p.error if p else None, ebay_profile_fetched_at=fetched_at if p else None,
        ))
    db.flush()
    stores = sync_system_stores(db)
    return {"import_id": batch.id, "rows": batch.rows, "rows_with_secret": batch.rows_with_secret,
            "replaced_rows": replaced, "file_sha256": batch.file_sha256[:12],
            "ebay_profiles_ok": sum(1 for p in profiles.values() if p.user_id),
            "ebay_profiles_failed": sum(1 for p in profiles.values() if not p.user_id), "system_stores": stores}


def sync_system_stores(db: Session) -> int:
    """为查到 eBay 账号的店铺建立/更新系统店铺：名称用 eBay 原始店铺名（没开店铺用卖家账号）。"""
    n = 0
    for c in db.scalars(select(StoreCredential).where(StoreCredential.store_id.is_not(None))):
        marketplace, tz = MARKETPLACES[c.platform]
        name = (c.ebay_store_name or c.ebay_user_id)[:200]
        store = db.get(Store, c.store_id)
        if store is None:
            db.add(Store(id=c.store_id, name=name, ebay_user_id=c.ebay_user_id, timezone=tz,
                         default_marketplace_id=marketplace))
        else:  # 包括 .env 单店铺时期建的店铺：站点、时区以凭证表的平台为准
            store.name, store.ebay_user_id = name, c.ebay_user_id
            store.default_marketplace_id, store.timezone = marketplace, tz
        n += 1
    db.flush()
    return n


def list_masked(db: Session) -> list[dict]:
    """只给界面看的脱敏清单：没有账号、IP、browserId、秘钥，也没有密文。"""
    rows = db.execute(select(StoreCredential, StoreCredentialImport.imported_at)
                      .join(StoreCredentialImport, StoreCredentialImport.id == StoreCredential.import_id)
                      .order_by(StoreCredential.seq, StoreCredential.id)).all()
    return [{"store_key": c.store_key, "seq": c.seq, "store_name": c.store_name, "platform": c.platform,
             "site_id": c.site_id, "has_secret": c.has_secret, "secret_fingerprint": c.secret_fingerprint,
             "store_id": c.store_id, "ebay_user_id": c.ebay_user_id, "ebay_store_name": c.ebay_store_name,
             "ebay_store_url": c.ebay_store_url, "ebay_profile_error": c.ebay_profile_error,
             "imported_at": at.isoformat(timespec="seconds") + "Z"} for c, at in rows]


def get_credential(db: Session, store_key: str) -> DecryptedCredential | None:
    """进程内解密一条记录（调用 eBay 接口时使用）；调用方不得记录或返回 secret。"""
    c = db.scalar(select(StoreCredential).where(StoreCredential.store_key == store_key))
    if c is None:
        return None
    data = json.loads(decrypt(get_settings().credentials_master_key, c.encrypted_payload, _aad(c.store_key)))
    return DecryptedCredential(store_key=c.store_key, store_name=c.store_name, platform=c.platform,
                               site_id=c.site_id, **data)


def get_credential_by_store_id(db: Session, store_id: str) -> DecryptedCredential | None:
    """按系统店铺 ID 解密（eBay 令牌管理用）；没有导入凭证表或该店不在表里时返回 None。"""
    key = db.scalar(select(StoreCredential.store_key).where(StoreCredential.store_id == store_id,
                                                            StoreCredential.has_secret.is_(True)))
    return get_credential(db, key) if key else None


def ebay_store_rows(db: Session) -> list[StoreCredential]:
    """可同步的 eBay 店铺：有秘钥、导入时查到了 eBay 账号并对应了系统店铺。"""
    return list(db.scalars(select(StoreCredential).where(
        StoreCredential.has_secret.is_(True), StoreCredential.store_id.is_not(None),
        StoreCredential.ebay_user_id.is_not(None)).order_by(StoreCredential.seq, StoreCredential.id)))


def find_by_secret(db: Session, secret: str) -> StoreCredential | None:
    """按秘钥指纹找店铺（不解密），用于核对 .env 里的 refresh token 属于哪个店铺。"""
    return db.scalar(select(StoreCredential).where(StoreCredential.secret_fingerprint == fingerprint(secret.strip())))
