"""会话标记：默认标记项、给会话打标记与备注、改与去掉、按标记筛选、只有管理员维护标记项、店铺范围、删会话时一起删。"""
from fastapi.testclient import TestClient

from conftest import login, make_user


def conv(store, key):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        c = get_or_create_conversation(db, store, key, "buyer_" + key, None, "EBAY_US", "simulator")
        _append_message(db, c, f"SIM-IN-{key}", "BUYER", "IN", "Does it fit my Golf?", None)
        db.flush()
        return c.id


def test_mark_conversation_and_filter(client):
    tags = client.get("/internal/message-tags").json()
    assert [t["name"] for t in tags][:3] == ["查适配中", "不适配", "已适配"]
    fit = next(t for t in tags if t["name"] == "查适配中")
    cid, other = conv("STORE_A", "C1"), conv("STORE_A", "C2")

    r = client.put(f"/internal/conversations/{cid}/mark", json={"tag_id": fit["id"], "note": "等买家发 VIN"})
    assert r.json()["tag"] == "查适配中" and r.json()["marked_by"] == "tester"
    assert client.get(f"/internal/conversations/{cid}").json()["conversation"]["mark"]["note"] == "等买家发 VIN"
    ids = [i["id"] for i in client.get("/internal/conversations", params={"group": "all", "tag_id": fit["id"]}).json()]
    assert ids == [cid] and other not in ids
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == cid)
    assert card["mark"]["tag"] == "查适配中"

    no_fit = next(t for t in tags if t["name"] == "不适配")
    assert client.put(f"/internal/conversations/{cid}/mark", json={"tag_id": no_fit["id"]}).json()["tag"] == "不适配"
    assert client.delete(f"/internal/conversations/{cid}/mark").json() == {"removed": 1}
    assert client.get(f"/internal/conversations/{cid}").json()["conversation"]["mark"] is None


def test_tags_admin_only_scope_and_delete_with_conversation(client, monkeypatch):
    from app import config

    b = conv("STORE_B", "C3")
    make_user("cs_tag", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_tag")
    assert cs.post("/internal/message-tags", json={"name": "新标记"}).status_code == 403
    tag_id = client.post("/internal/message-tags", json={"name": "等补发", "color": "danger"}).json()["id"]
    assert client.post("/internal/message-tags", json={"name": "等补发"}).status_code == 409
    assert cs.put(f"/internal/conversations/{b}/mark", json={"tag_id": tag_id}).status_code == 404  # 不是自己的店铺

    client.put(f"/internal/conversations/{b}/mark", json={"tag_id": tag_id})
    monkeypatch.setenv("TEST_DELETE_ENABLED", "true")
    config.get_settings.cache_clear()
    assert client.delete(f"/internal/conversations/{b}").status_code == 200  # 有标记的会话也能删（标记一起删）
    client.patch(f"/internal/message-tags/{tag_id}", json={"is_active": False})
    assert all(t["id"] != tag_id for t in cs.get("/internal/message-tags").json())
