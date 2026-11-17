"""工作台回复带图片：上传（只收图片、限大小）、预览、发送时带上；对接 eBay 的店铺给 eBay 公网地址，没有公网地址就不发。"""
import base64

from app import config
from test_outbox import _ebay_conv
from test_pipeline import detail

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def test_upload_preview_and_send_with_images(client, monkeypatch, tmp_path):
    from app.ebay import auth as ebay_auth
    from app.ebay import message_api
    from app.sending import channel as ch

    monkeypatch.setenv("AFTERSALES_FILES_DIR", tmp_path.as_posix())
    monkeypatch.setenv("SEND_DELAY_SECONDS", "0")
    config.get_settings.cache_clear()
    local = _ebay_conv("STORE_B")  # 没对接真实收发的店铺：图片只记在本地

    up = client.post(f"/internal/conversations/{local}/reply-images", json={"name": "vin.png", "data": PNG}).json()
    assert client.get(up["url"]).status_code == 200
    assert client.post(f"/internal/conversations/{local}/reply-images",
                       json={"data": base64.b64encode(b"not an image at all").decode()}).status_code == 422
    other = _ebay_conv("STORE_A")
    assert client.get(f"/internal/conversations/{other}/reply-images/{up['file_id']}").status_code == 404
    # 别的会话的图片不能拿来发
    assert client.post(f"/internal/conversations/{other}/send", json={"text": "Hi", "file_ids": [up["file_id"]]}).status_code == 422

    r = client.post(f"/internal/conversations/{local}/send", json={"text": "See the photo.", "file_ids": [up["file_id"]]})
    assert r.status_code == 200
    last = detail(client, local)["messages"][-1]
    assert last["body"] == "See the photo." and last["media"][0]["fileId"] == up["file_id"]

    # 对接 eBay 的店铺：没有公网地址不发；有了就把地址交给 eBay
    sent = []
    monkeypatch.setattr(message_api, "send_message", lambda cli, **kw: sent.append(kw) or {"messageId": "EB-IMG-1"})
    monkeypatch.setattr(ebay_auth, "for_store", lambda *a, **k: None)
    monkeypatch.setattr(ch, "check_public_url", lambda u: None)
    monkeypatch.setenv("EBAY_CHANNEL", "ebay")
    monkeypatch.setenv("EBAY_LIVE_STORES", "STORE_A")
    monkeypatch.setenv("EBAY_NOTIFY_ENDPOINT", "")
    monkeypatch.setenv("PUBLIC_MEDIA_BASE_URL", "")
    config.get_settings.cache_clear()
    up2 = client.post(f"/internal/conversations/{other}/reply-images", json={"name": "a.png", "data": PNG}).json()
    r = client.post(f"/internal/conversations/{other}/send", json={"text": "Photo", "file_ids": [up2["file_id"]]})
    assert r.status_code == 422 and "公网地址" in r.json()["detail"]["message"] and not sent

    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("EBAY_NOTIFY_ENDPOINT", "https://hook.example.com/webhooks/ebay/notification")
    config.get_settings.cache_clear()
    r = client.post(f"/internal/conversations/{other}/send", json={"text": "Photo", "file_ids": [up2["file_id"]]})
    assert r.status_code == 200, r.text
    assert sent[0]["media"][0]["mediaType"] == "IMAGE" and sent[0]["media"][0]["mediaUrl"].startswith("https://hook.example.com/webhooks/media/")
    assert client.post(f"/internal/conversations/{other}/send", json={"text": "x", "file_ids": [1, 2, 3, 4, 5, 6]}).status_code == 422
