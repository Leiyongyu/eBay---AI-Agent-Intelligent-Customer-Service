"""店铺凭证：加密入库、整表覆盖、禁止修改、接口不泄露明文与密文。数据全部是虚构的。"""
import pytest
from openpyxl import Workbook
from sqlalchemy import select, update
from sqlalchemy.exc import DatabaseError

from app.config import get_settings
from app.db import session_scope
from app.models import StoreCredential
from app.security import crypto
from app.security.store_credentials import (
    CredentialImportError,
    get_credential,
    import_credentials,
    list_masked,
    make_store_key,
)

HEADER = ["序号", "店铺名称", "平台", "店铺账号", "店铺IP", "IP已过期", "browserId", "站点ID", "秘钥"]
SECRET_A = "v^1.1#i^1#FAKE-TOKEN-A-" + "x" * 73
def fake_profile(secret):
    from app.ebay.profile import SellerProfile

    if secret.startswith("BAD"):
        return SellerProfile(error="换取访问令牌失败：invalid_grant")
    return SellerProfile(user_id="seller-" + secret[21], store_name="Store " + secret[21], registration_site="Germany")


SECRET_B = "v^1.1#i^1#FAKE-TOKEN-B-" + "y" * 73


def make_xlsx(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture()
def master_key(app_env, monkeypatch):
    key = crypto.new_master_key()
    monkeypatch.setattr(get_settings(), "credentials_master_key", key)
    return key


def test_import_encrypts_and_decrypts(master_key, tmp_path):
    f = make_xlsx(tmp_path / "stores.xlsx", [
        [1, "Shop-A", "eBay-德国", "seller-a", "10.0.0.1", "否", "B0000000000001", "23", SECRET_A],
        [2, "Shop-B", "亚马逊-美国", "seller-b", "10.0.0.2", "否", "B0000000000002", "1", None],
    ])
    with session_scope() as db:
        summary = import_credentials(db, f, actor="test", profile_fetcher=fake_profile)
        assert summary["rows"] == 2 and summary["rows_with_secret"] == 1
    with session_scope() as db:
        row = db.scalar(select(StoreCredential).where(StoreCredential.store_name == "Shop-A"))
        # 库里看不到任何明文
        for plain in (SECRET_A, "seller-a", "10.0.0.1", "B0000000000001"):
            assert plain not in row.encrypted_payload
        cred = get_credential(db, make_store_key("Shop-A", "eBay-德国"))
        assert cred.secret == SECRET_A and cred.account == "seller-a" and cred.browser_id == "B0000000000001"
        assert SECRET_A not in repr(cred)
        masked = list_masked(db)
        assert {m["store_name"] for m in masked} == {"Shop-A", "Shop-B"}
        assert all("encrypted_payload" not in m and "secret" not in m for m in masked)


def test_wrong_key_and_moved_ciphertext_fail(master_key):
    token = crypto.encrypt(master_key, b"secret", "store_credentials:ZN-1:k1")
    with pytest.raises(crypto.CredentialCryptoError):
        crypto.decrypt(crypto.new_master_key(), token, "store_credentials:ZN-1:k1")
    with pytest.raises(crypto.CredentialCryptoError):  # 密文挪到别的记录上
        crypto.decrypt(master_key, token, "store_credentials:ZN-2:k1")


def test_overwrite_only_no_update(master_key, tmp_path):
    with session_scope() as db:
        import_credentials(db, make_xlsx(tmp_path / "v1.xlsx", [[1, "Shop-A", "eBay-德国", "a", "1.1.1.1", "否", "B1", "23", SECRET_A]]), "test", profile_fetcher=fake_profile)
    with session_scope() as db:
        summary = import_credentials(db, make_xlsx(tmp_path / "v2.xlsx", [
            [1, "Shop-A", "eBay-德国", "a", "1.1.1.1", "否", "B1", "23", SECRET_B],
            [2, "Shop-C", "eBay-英国", "c", "1.1.1.3", "否", "B3", "4", SECRET_A],
        ]), "test", profile_fetcher=fake_profile)
        assert summary["replaced_rows"] == 1
    with session_scope() as db:
        assert get_credential(db, make_store_key("Shop-A", "eBay-德国")).secret == SECRET_B  # 被新文件覆盖
        assert len(list_masked(db)) == 2
    with pytest.raises(DatabaseError, match="不允许修改"):
        with session_scope() as db:
            db.execute(update(StoreCredential).values(store_name="hacked"))


def test_bad_file_changes_nothing(master_key, tmp_path):
    with session_scope() as db:
        import_credentials(db, make_xlsx(tmp_path / "ok.xlsx", [[1, "Shop-A", "eBay-德国", "a", "", "否", "B1", "23", SECRET_A]]), "test", profile_fetcher=fake_profile)
    dup = make_xlsx(tmp_path / "dup.xlsx", [[1, "Shop-A", "eBay-德国", "a", "", "否", "B1", "23", SECRET_A],
                                             [2, "Shop-A", "eBay-德国", "a", "", "否", "B1", "23", SECRET_B]])
    with pytest.raises(CredentialImportError, match="重复"):
        with session_scope() as db:
            import_credentials(db, dup, "test", profile_fetcher=fake_profile)
    with session_scope() as db:
        assert len(list_masked(db)) == 1  # 原数据不受影响


def test_api_never_returns_secrets(client, master_key, tmp_path):
    with session_scope() as db:
        import_credentials(db, make_xlsx(tmp_path / "s.xlsx", [[1, "Shop-A", "eBay-德国", "seller-a", "10.0.0.1", "否", "B1", "23", SECRET_A]]), "test", profile_fetcher=fake_profile)
    r = client.get("/api/system/store-credentials")
    assert r.status_code == 200
    body = r.text
    for plain in (SECRET_A, "seller-a", "10.0.0.1"):
        assert plain not in body
    with session_scope() as db:
        payload = db.scalar(select(StoreCredential.encrypted_payload))
    assert payload not in body
    assert r.json()["stores"][0]["has_secret"] is True


def test_profiles_fill_columns_and_store_filter(client, master_key, tmp_path):
    f = make_xlsx(tmp_path / "s.xlsx", [
        [1, "Shop-A", "eBay-德国", "a", "", "否", "B1", "23", SECRET_A],
        [2, "Shop-B", "eBay-英国", "b", "", "否", "B2", "4", "BAD" + SECRET_B[3:]],
        [3, "Shop-C", "亚马逊-美国", "c", "", "否", "B3", "1", None],
    ])
    with session_scope() as db:
        s = import_credentials(db, f, "test", profile_fetcher=fake_profile)
        assert s["ebay_profiles_ok"] == 1 and s["ebay_profiles_failed"] == 1 and s["system_stores"] == 1
    creds = {c["store_name"]: c for c in client.get("/api/system/store-credentials").json()["stores"]}
    assert creds["Shop-A"]["ebay_user_id"] == "seller-A" and creds["Shop-A"]["ebay_store_name"] == "Store A"
    assert creds["Shop-B"]["ebay_user_id"] is None and "invalid_grant" in creds["Shop-B"]["ebay_profile_error"]
    assert creds["Shop-C"]["ebay_user_id"] is None and creds["Shop-C"]["ebay_profile_error"] is None  # 非 eBay 不查
    stores = {s["id"]: s for s in client.get("/internal/stores").json()}
    sid = creds["Shop-A"]["store_id"]
    assert stores[sid]["name"] == "Store A" and stores[sid]["default_marketplace_id"] == "EBAY_DE"
