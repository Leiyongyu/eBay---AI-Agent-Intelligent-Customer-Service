"""要对接的 eBay 店铺清单（多店铺）。

- 导入过店铺凭证（store_credentials）时：其中有秘钥、查到 eBay 账号的店铺全部对接，各用各的秘钥；
- 没导入时：退回 .env 里配置的单个店铺（EBAY_STORE_ID + EBAY_REFRESH_TOKEN），兼容原有配置；
- EBAY_SYNC_STORES 可填店铺 ID 或 eBay 账号（逗号分隔）只同步其中几个，留空为全部。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import get_settings
from ..db import session_scope
from ..models import Store


@dataclass(frozen=True)
class EbayStoreTarget:
    store_id: str
    name: str
    seller_username: str | None  # 店铺自己的 eBay 账号：区分买家/店铺发言、校验刊登归属
    marketplace_id: str
    source: str  # credentials：来自凭证表 / env：.env 单店铺


def ebay_store_targets() -> list[EbayStoreTarget]:
    from ..security.store_credentials import ebay_store_rows

    s = get_settings()
    targets: list[EbayStoreTarget] = []
    with session_scope() as db:
        for c in ebay_store_rows(db):
            store = db.get(Store, c.store_id)
            targets.append(EbayStoreTarget(
                store_id=c.store_id, name=(store.name if store else c.ebay_store_name or c.ebay_user_id),
                seller_username=c.ebay_user_id,
                marketplace_id=store.default_marketplace_id if store else s.ebay_store_marketplace,
                source="credentials"))
        if not targets and s.ebay_refresh_token:
            from .sync import ensure_ebay_store

            store = ensure_ebay_store(db)
            targets.append(EbayStoreTarget(store_id=store.id, name=store.name,
                                           seller_username=s.ebay_seller_username or store.ebay_user_id,
                                           marketplace_id=store.default_marketplace_id, source="env"))
    # 只拉这些店铺：EBAY_SYNC_STORES（* = 全部店铺）；没填时用 EBAY_LIVE_STORES（对接 eBay 真实收发的店铺）。按店铺 ID、eBay 账号或店铺名匹配。
    # 拉取范围和真实发送范围分开：拉进来的其他店铺发送只记在本地，不发到 eBay（发送只看 EBAY_LIVE_STORES）
    if (s.ebay_sync_stores or "").strip() == "*":
        return targets
    wanted = {x.strip().lower() for x in (s.ebay_sync_stores or "").split(",") if x.strip()} or s.ebay_live_store_keys
    if wanted:
        targets = [t for t in targets if t.store_id.lower() in wanted or (t.seller_username or "").lower() in wanted
                   or (t.name or "").lower() in wanted]
    return targets


def ebay_not_ready_reasons() -> list[str]:
    """多店铺版的就绪检查：应用凭证必须有；店铺秘钥来自凭证表或 .env 二选一。"""
    s = get_settings()
    reasons = [f"{name} 未配置" for name, v in (("EBAY_CLIENT_ID", s.ebay_client_id),
                                                ("EBAY_CLIENT_SECRET", s.ebay_client_secret)) if not v]
    if s.ebay_env not in ("production", "sandbox"):
        reasons.append(f"EBAY_ENV={s.ebay_env} 无效（production / sandbox）")
    if not reasons and not ebay_store_targets():
        reasons.append("没有可对接的 eBay 店铺：请运行 scripts/import_store_credentials.py 导入店铺凭证，"
                       "或在 .env 填 EBAY_REFRESH_TOKEN")
    return reasons


def is_ebay_store(store_id: str) -> bool:
    """只读判断（不建店铺行，避免在 Agent 运行的事务里写库）。"""
    from sqlalchemy import select

    from ..models import StoreCredential

    s = get_settings()
    with session_scope() as db:
        if db.scalar(select(StoreCredential.id).where(StoreCredential.store_id == store_id,
                                                      StoreCredential.has_secret.is_(True)).limit(1)):
            return True
    return bool(store_id == s.ebay_store_id and s.ebay_refresh_token and s.ebay_client_id)
