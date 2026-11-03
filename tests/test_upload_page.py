"""买家上传页：订单校验（邮编/用户名/签名链接、人机验证、限流）、表单、文件登记与限额、分块签名与合并核对、提交、
后台从 R2 拉回存进店铺文件夹并关联会话标“待售后”、清理、工作台展示与权限。R2 用内存里的假存储，不连真实存储。"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import config
from conftest import login, make_user

ORDER = "12-34567-89012"


class FakeR2:
    def __init__(self):
        self.mp: dict[str, dict] = {}
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.aborted: list[str] = []

    def create_multipart(self, key, content_type):
        uid = f"mp{len(self.mp) + len(self.aborted) + len(self.objects) + 1}"
        self.mp[uid] = {"key": key, "parts": {}}
        return uid

    def presign_part(self, key, upload_id, part_number, expires=900):
        return f"https://r2.test/{key}?partNumber={part_number}&uploadId={upload_id}&X-Amz-Expires={expires}"

    def put(self, upload_id, n, data):  # 模拟买家浏览器 PUT 一个分块
        self.mp[upload_id]["parts"][n] = data
        return f'"etag{n}"'

    def list_parts(self, key, upload_id):
        return [{"part_number": n, "etag": f'"etag{n}"', "size": len(d)} for n, d in sorted(self.mp[upload_id]["parts"].items())]

    def complete(self, key, upload_id, parts):
        mp = self.mp.pop(upload_id)
        self.objects[key] = b"".join(mp["parts"][p["part_number"]] for p in sorted(parts, key=lambda p: p["part_number"]))

    def abort(self, key, upload_id):
        self.mp.pop(upload_id, None)
        self.aborted.append(upload_id)

    def size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def read(self, key, start=0):
        data = self.objects[key][start:]
        for i in range(0, len(data), 3):
            yield data[i:i + 3]

    def delete(self, key):
        self.objects.pop(key, None)
        self.deleted.append(key)

    def stale_multiparts(self, older_than):
        return []


def fake_order(**kw):
    o = {"orderId": ORDER, "buyer": {"username": "buyer_up"},
         "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {"contactAddress": {"postalCode": "87435", "countryCode": "DE"}}}}],
         "lineItems": [{"legacyItemId": "157849405143", "title": "Handbremse Steuerelement", "quantity": 1},
                       {"legacyItemId": "157849405144", "title": "Bremssattel", "quantity": 2}]}
    o.update(kw)
    return o


@pytest.fixture()
def up_env(client, monkeypatch, tmp_path):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.upload_page import guard, r2 as r2mod, service

    for k, v in {"UPLOAD_PAGE_ENABLED": "true", "R2_ACCOUNT_ID": "acc", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
                 "R2_BUCKET": "bucket", "TURNSTILE_SITE_KEY": "site-key", "TURNSTILE_SECRET_KEY": "secret",
                 "UPLOAD_LINK_SECRET": "link-secret", "AFTERSALES_FILES_DIR": (tmp_path / "files").as_posix(),
                 "AFTERSALES_VIRUS_SCAN": "false"}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    fake = FakeR2()
    r2mod.set_r2_override(fake)
    guard.set_turnstile_override(lambda token, ip: token == "ok")
    guard.limiter.reset()
    monkeypatch.setattr(service, "part_size", lambda: 4)  # 测试用 4 字节一块
    monkeypatch.setattr(service, "lookup_order_api", lambda order_id: ("STORE_A", "buyer_up") if order_id == ORDER else None)
    monkeypatch.setattr(service, "fetch_order", lambda store_id, order_id: fake_order() if order_id == ORDER else None)
    with session_scope() as db:
        c = get_or_create_conversation(db, "STORE_A", "SIM-UP-1", "buyer_up", None, "EBAY_DE", "simulator")
        _append_message(db, c, "SIM-IN-UP-1", "BUYER", "IN", "Das Teil passt nicht", None)
        db.flush()
        conv_id = c.id
    buyer = TestClient(client.app)  # 买家：不登录
    yield client, buyer, fake, conv_id
    r2mod.set_r2_override(None)
    guard.set_turnstile_override(None)


def verified(buyer, **kw):
    body = {"order_id": ORDER, "zip": "87 435", "turnstile_token": "ok", "language": "de", **kw}
    r = buyer.post("/upload/api/verify", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def upload_file(buyer, fake, session, name, data, ctype="video/mp4"):
    h = {"X-Upload-Session": session}
    reg = buyer.post("/upload/api/files", headers=h, json={"name": name, "size": len(data), "type": ctype})
    assert reg.status_code == 200, reg.text
    fid, parts = reg.json()["file_id"], reg.json()["parts"]
    signed = buyer.post(f"/upload/api/files/{fid}/parts", headers=h, json={"part_numbers": list(range(1, parts + 1))}).json()
    assert "partNumber=1" in signed["urls"]["1"] and "X-Amz-Expires=900" in signed["urls"]["1"]
    from app.db import session_scope
    from app.models import AftersalesFile

    with session_scope() as db:
        mp = db.get(AftersalesFile, fid).r2_upload_id
    etags = [{"part_number": n, "etag": fake.put(mp, n, data[(n - 1) * 4:n * 4])} for n in range(1, parts + 1)]
    done = buyer.post(f"/upload/api/files/{fid}/complete", headers=h, json={"parts": etags})
    assert done.status_code == 200 and done.json()["uploaded"], done.text
    return fid


def test_page_and_closed(up_env, monkeypatch):
    _, buyer, _, _ = up_env
    html = buyer.get("/upload/").text
    assert 'data-sitekey="site-key"' in html and "const OPEN = true" in html
    monkeypatch.setenv("UPLOAD_PAGE_ENABLED", "false")
    config.get_settings.cache_clear()
    assert "const OPEN = false" in buyer.get("/upload/").text
    assert buyer.post("/upload/api/verify", json={"order_id": ORDER}).status_code == 503


def test_verify_order_zip_username_captcha_and_rate(up_env):
    _, buyer, _, _ = up_env
    assert buyer.post("/upload/api/verify", json={"order_id": ORDER, "zip": "87435", "turnstile_token": "bad"}).json()["code"] == "captcha"
    assert buyer.post("/upload/api/verify", json={"order_id": "123", "zip": "1", "turnstile_token": "ok"}).json()["code"] == "order_format"
    wrong = buyer.post("/upload/api/verify", json={"order_id": ORDER, "zip": "99999", "turnstile_token": "ok"})
    assert wrong.status_code == 403 and wrong.json()["code"] == "mismatch"
    st = verified(buyer)
    assert st["store_name"] and [i["item_id"] for i in st["items"]] == ["157849405143", "157849405144"]
    assert "postalCode" not in str(st) and "87435" not in str(st)  # 不把收货信息返回给页面
    assert verified(buyer, zip=None, username="BUYER_UP")["session"]
    for _ in range(10):
        buyer.post("/upload/api/verify", json={"order_id": ORDER, "zip": "0", "turnstile_token": "ok"})
    assert buyer.post("/upload/api/verify", json={"order_id": ORDER, "zip": "87435", "turnstile_token": "ok"}).json()["code"] == "rate"


def test_full_flow_pull_back_and_workbench(up_env, tmp_path):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import BuyerUpload, Conversation, ConversationEvent
    from app.upload_page.service import pull_once

    client, buyer, fake, conv_id = up_env
    st = verified(buyer)
    h = {"X-Upload-Session": st["session"]}
    assert buyer.post("/upload/api/submit", headers=h).json()["code"] == "issue"  # 还没填问题
    form = {"issue_type": "NOT_FIT", "description": "Passt nicht an meinen Golf 7", "installed": "TRIED",
            "vehicle": {"make": "VW", "model": "Golf 7", "year": "2016", "hacker": "x"}, "item_id": "157849405143",
            "item_title": "Handbremse Steuerelement"}
    assert buyer.post("/upload/api/form", headers=h, json=form).json() == {"ok": True}
    assert buyer.post("/upload/api/files", headers=h, json={"name": "a.exe", "size": 10}).json()["code"] == "type"
    assert buyer.post("/upload/api/files", headers=h, json={"name": "a.mp4", "size": 3 * 1024 ** 3}).json()["code"] == "too_large"
    assert buyer.post("/upload/api/submit", headers=h).json()["code"] == "no_files"

    video = b"0123456789abcdef!"  # 17 字节 → 5 块
    fid = upload_file(buyer, fake, st["session"], "Fehler.mp4", video)
    # 只登记没传完的文件：提交被拒
    pending = buyer.post("/upload/api/files", headers=h, json={"name": "b.jpg", "size": 5, "type": "image/jpeg"}).json()["file_id"]
    assert buyer.post(f"/upload/api/files/{pending}/complete", headers=h,
                      json={"parts": [{"part_number": 1, "etag": "x"}]}).json()["code"] == "incomplete"
    assert buyer.post("/upload/api/submit", headers=h).json()["code"] == "uploading"
    assert buyer.delete(f"/upload/api/files/{pending}", headers=h).json() == {"ok": True}
    sub = buyer.post("/upload/api/submit", headers=h).json()
    assert sub["code"].startswith("AS-") and sub["status"] == "SUBMITTED"
    assert buyer.post("/upload/api/form", headers=h, json=form).json()["code"] == "submitted"

    assert pull_once() == {"fetched": 1, "failed": 0}
    with session_scope() as db:
        up = db.scalar(select(BuyerUpload).where(BuyerUpload.code == sub["code"]))
        assert up.status == "RECEIVED" and up.vehicle == {"make": "VW", "model": "Golf 7", "year": "2016"}
        conv = db.get(Conversation, conv_id)
        assert (conv.aftersales_status, conv.aftersales_type, conv.aftersales_marked_by) == ("PENDING", "不适配", "上传页")
        assert sub["code"] in conv.aftersales_note and "Golf 7" in conv.aftersales_note
        assert db.scalar(select(ConversationEvent.event_type).where(ConversationEvent.conversation_id == conv_id)) == \
            "BUYER_UPLOAD_RECEIVED"
    files = list((tmp_path / "files").rglob("*.mp4"))
    assert len(files) == 1 and files[0].read_bytes() == video and files[0].name.startswith(f"{ORDER}_")
    assert files[0].parent.name.startswith("STORE_A")
    assert fake.objects == {} and len(fake.deleted) == 1  # R2 里的文件拉回后删掉

    # 工作台：会话附件区、售后附件页“上传页”来源、预览
    conv_files = client.get(f"/internal/conversations/{conv_id}/aftersales-files").json()
    upl = conv_files["uploads"][0]
    assert (upl["code"], upl["issue_label"], upl["installed"]) == (sub["code"], "不适配", "试装过")
    assert upl["files"][0]["status"] == "STORED"
    listed = client.get("/internal/aftersales-files", params={"source": "upload"}).json()
    assert [i["code"] for i in listed["items"]] == [sub["code"]]
    assert client.get("/internal/aftersales-files").json()["items"][0]["kind"] == "upload"  # 全部来源里也有
    assert client.get(f"/internal/aftersales-files/{fid}/content").content == video
    make_user("cs_up", "CS", stores=["STORE_B"])
    cs = TestClient(client.app)
    login(cs, "cs_up")
    assert cs.get(f"/internal/aftersales-files/{fid}/content").status_code == 403  # 没开“可看售后附件”


def test_order_quota_and_size_mismatch(up_env, monkeypatch):
    client, buyer, fake, _ = up_env
    monkeypatch.setenv("UPLOAD_ORDER_MAX_GB", str(20 / 1024 ** 3))  # 这个订单最多 20 字节
    config.get_settings.cache_clear()
    st = verified(buyer)
    h = {"X-Upload-Session": st["session"]}
    buyer.post("/upload/api/form", headers=h, json={"issue_type": "DAMAGED"})
    upload_file(buyer, fake, st["session"], "a.jpg", b"0123456789abc", "image/jpeg")
    assert buyer.post("/upload/api/files", headers=h, json={"name": "b.jpg", "size": 10}).json()["code"] == "order_quota"
    # 合并后大小和登记的不一致：丢弃
    reg = buyer.post("/upload/api/files", headers=h, json={"name": "c.jpg", "size": 4, "type": "image/jpeg"}).json()
    from app.db import session_scope
    from app.models import AftersalesFile

    with session_scope() as db:
        mp = db.get(AftersalesFile, reg["file_id"]).r2_upload_id
    etag = fake.put(mp, 1, b"12")
    r = buyer.post(f"/upload/api/files/{reg['file_id']}/complete", headers=h, json={"parts": [{"part_number": 1, "etag": etag}]})
    assert r.json()["code"] == "size"
    with session_scope() as db:
        assert db.get(AftersalesFile, reg["file_id"]).status == "DOWNLOAD_FAILED"


def test_link_from_workbench_and_forged_token(up_env):
    from app.upload_page import guard

    client, buyer, fake, conv_id = up_env
    r = client.post(f"/internal/conversations/{conv_id}/upload-link", json={"order_id": ORDER})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("https://upload.diwhx.xyz/upload/?t=")
    token = url.split("t=", 1)[1]
    st = buyer.post("/upload/api/verify", json={"link_token": token}).json()  # 不用再校验订单和人机验证
    assert st["order_id"] == ORDER and st["session"]
    assert client.get(f"/internal/conversations/{conv_id}/aftersales-files").json()["pending_links"][0]["order_id"] == ORDER
    forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert buyer.post("/upload/api/verify", json={"link_token": forged}).json()["code"] == "link"
    from datetime import datetime, timezone

    expired = guard.sign_link(1, datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert buyer.post("/upload/api/verify", json={"link_token": expired}).json()["code"] == "link"
    assert client.post(f"/internal/conversations/{conv_id}/upload-link", json={"order_id": "99-99999-99999"}).status_code == 404


def test_cleanup_abandoned_and_expired(up_env):
    from app.aftersales_mail.service import cleanup
    from app.db import session_scope, utcnow
    from app.models import AftersalesFile, BuyerUpload

    _, buyer, fake, _ = up_env
    st = verified(buyer)
    h = {"X-Upload-Session": st["session"]}
    fid = buyer.post("/upload/api/files", headers=h, json={"name": "big.mp4", "size": 8}).json()["file_id"]
    with session_scope() as db:
        f = db.get(AftersalesFile, fid)
        f.received_at = utcnow() - timedelta(days=8)
        for up in db.query(BuyerUpload):
            up.created_at = utcnow() - timedelta(days=8)
    with session_scope() as db:
        r = cleanup(db)
        assert (r["upload_abandoned"], r["upload_expired"]) == (1, 1)
        assert db.get(AftersalesFile, fid).r2_state == "ABORTED" and fake.aborted
    assert buyer.get("/upload/api/state", headers=h).json()["code"] == "session"  # 过期后会话失效
