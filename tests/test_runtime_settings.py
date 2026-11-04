"""系统配置页：只有管理员能看能改；页面配置优先于 .env、立即生效；密码只写不读；恢复 .env。"""
from fastapi.testclient import TestClient

from conftest import login, make_user


def item(items, key):
    return next(i for i in items if i["key"] == key)


def test_admin_settings_override_env_and_reset(client):
    from app.config import get_settings

    items = client.get("/internal/admin/settings").json()["items"]
    assert item(items, "invoice_auto_send")["source"] == "env" and item(items, "invoice_smtp_password")["value"] is None

    r = client.put("/internal/admin/settings", json={"values": {
        "invoice_auto_send": True, "invoice_test_recipient": " test@qq.example ", "invoice_smtp_password": "s3cret",
        "auto_reply_max_streak": "4"}})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["changed"]) == ["auto_reply_max_streak", "invoice_auto_send", "invoice_smtp_password",
                                           "invoice_test_recipient"]
    s = get_settings()
    assert (s.invoice_auto_send, s.invoice_test_recipient, s.invoice_smtp_password, s.auto_reply_max_streak) == (
        True, "test@qq.example", "s3cret", 4)
    pw = item(r.json()["items"], "invoice_smtp_password")
    assert pw["value"] is None and pw["is_set"] and "s3cret" not in r.text  # 密码不回传
    # 密码留空 = 不改
    client.put("/internal/admin/settings", json={"values": {"invoice_smtp_password": ""}})
    assert get_settings().invoice_smtp_password == "s3cret"

    assert client.put("/internal/admin/settings", json={"values": {"database_url": "x"}}).status_code == 422
    assert client.put("/internal/admin/settings", json={"values": {"auto_reply_max_streak": "abc"}}).status_code == 422
    assert client.put("/internal/admin/settings", json={"values": {"auto_send_max_risk": "L3"}}).status_code == 422

    # 恢复 .env；重启（重新装载）后页面配置照样生效
    client.delete("/internal/admin/settings/invoice_auto_send")
    assert get_settings().invoice_auto_send is False
    from app import runtime_settings

    runtime_settings.load()
    assert get_settings().invoice_test_recipient == "test@qq.example"


def test_settings_admin_only(client):
    make_user("cs_cfg", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_cfg")
    assert cs.get("/internal/admin/settings").status_code == 403
    assert cs.put("/internal/admin/settings", json={"values": {"invoice_auto_send": True}}).status_code == 403
