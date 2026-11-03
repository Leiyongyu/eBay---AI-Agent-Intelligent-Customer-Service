"""店铺凭证覆盖导入：PyCharm 里直接 Run 本文件。

读取 .env 的 STORE_CREDENTIALS_FILE（紫鸟店铺全量 Excel，第一行为表头：序号、店铺名称、平台、店铺账号、店铺IP、
IP已过期、browserId、站点ID、秘钥），加密后**整表覆盖**写入数据库表 store_credentials；带秘钥的 eBay 店铺会先用该秘钥只读查询 eBay 卖家账号和原始店铺名
（GetUser / GetStore）一并写入，并同步为工作台店铺筛选里的店铺：
- 店铺账号、店铺 IP、IP 是否过期、browserId、秘钥整体 AES-256-GCM 加密；主密钥 CREDENTIALS_MASTER_KEY 只在 .env。
- 首次运行 .env 没有主密钥时自动生成并写回 .env。**请另外备份这把主密钥**：丢失后无法解密，只能用原始 Excel 重新导入。
- 每次运行都会删除表中全部旧记录、换成文件里的内容；数据库触发器禁止单条修改。
- 本脚本不打印任何账号、IP、秘钥；导入成功后原始 Excel 仍是明文，请妥善保管。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from app.config import ENV_FILE, get_settings  # noqa: E402
from app.security.crypto import new_master_key  # noqa: E402


def ensure_master_key() -> None:
    s = get_settings()
    if s.credentials_master_key:
        return
    key = new_master_key()
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    if re.search(r"^CREDENTIALS_MASTER_KEY=", text, re.M):
        text = re.sub(r"^CREDENTIALS_MASTER_KEY=.*$", f"CREDENTIALS_MASTER_KEY={key}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\nCREDENTIALS_MASTER_KEY={key}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    s.credentials_master_key = key
    print("⚠ 已生成加密主密钥 CREDENTIALS_MASTER_KEY 并写入 .env。请另外备份它（如密码管理器）：丢失后无法解密，只能重新导入。")


def main() -> int:
    s = get_settings()
    if not s.store_credentials_file:
        print("❌ 请在 .env 填写 STORE_CREDENTIALS_FILE（紫鸟店铺全量 Excel 的路径）")
        return 1
    path = Path(s.store_credentials_file)
    if not path.exists():
        print(f"❌ 文件不存在：{path}")
        return 1
    ensure_master_key()

    from app.db import init_db, session_scope
    from app.security.store_credentials import CredentialImportError, find_by_secret, import_credentials, list_masked

    init_db()
    try:
        with session_scope() as db:
            summary = import_credentials(
                db, path, actor="import_store_credentials.py",
                progress=lambda i, n, name: print(f"   查询 eBay 店铺信息 {i}/{n}：{name}", flush=True))
    except CredentialImportError as e:
        print(f"❌ 文件格式不对，未做任何修改：{e}")
        return 1
    print(f"✅ 覆盖导入完成：{summary['rows']} 个店铺（带秘钥 {summary['rows_with_secret']} 个），"
          f"替换旧记录 {summary['replaced_rows']} 条；文件指纹 {summary['file_sha256']}")
    print(f"   eBay 店铺信息：成功 {summary['ebay_profiles_ok']} 个，失败 {summary['ebay_profiles_failed']} 个；"
          f"工作台店铺筛选已同步 {summary['system_stores']} 个店铺")
    with session_scope() as db:
        stores = list_masked(db)
        by_platform = Counter((r["platform"], r["has_secret"]) for r in stores)
        for platform in sorted({p for p, _ in by_platform}):
            print(f"   {platform}：{by_platform[(platform, True)]} 个有秘钥 / {by_platform[(platform, True)] + by_platform[(platform, False)]} 个")
        if s.ebay_refresh_token:
            cur = find_by_secret(db, s.ebay_refresh_token)
            print(f"   .env 当前的 EBAY_REFRESH_TOKEN 属于：{cur.store_name if cur else '（表中没有这个秘钥）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
