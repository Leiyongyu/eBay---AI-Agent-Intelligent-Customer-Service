"""会话标记：默认标记项、一个会话多个标记与备注、单独去掉、按标记筛选、所有账号都能新建/删除标记项（管理员改名停用）、店铺范围、删会话时一起删。"""
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
    assert [(m["tag"], m["marked_by"]) for m in r.json()] == [("查适配中", "tester")]
    assert client.get(f"/internal/conversations/{cid}").json()["conversation"]["mark"]["note"] == "等买家发 VIN"
    ids = [i["id"] for i in client.get("/internal/conversations", params={"group": "all", "tag_id": fit["id"]}).json()]
    assert ids == [cid] and other not in ids
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == cid)
    assert card["mark"]["tag"] == "查适配中"

    # 一个会话可以打多个标记；同一个标记再打一次是改备注，不会重复
    no_fit = next(t for t in tags if t["name"] == "不适配")
    marks = client.put(f"/internal/conversations/{cid}/mark", json={"tag_id": no_fit["id"]}).json()
    assert [m["tag"] for m in marks] == ["查适配中", "不适配"]
    marks = client.put(f"/internal/conversations/{cid}/mark", json={"tag_id": fit["id"], "note": "VIN 已收到"}).json()
    assert len(marks) == 2 and marks[0]["note"] == "VIN 已收到"
    card = next(i for i in client.get("/internal/conversations", params={"group": "all"}).json() if i["id"] == cid)
    assert [m["tag"] for m in card["marks"]] == ["查适配中", "不适配"]
    # 单独去掉一个，再全部去掉
    r = client.delete(f"/internal/conversations/{cid}/mark", params={"tag_id": fit["id"]}).json()
    assert r["removed"] == 1 and [m["tag"] for m in r["marks"]] == ["不适配"]
    assert client.delete(f"/internal/conversations/{cid}/mark").json() == {"removed": 1, "marks": []}
    assert client.get(f"/internal/conversations/{cid}").json()["conversation"]["mark"] is None


def test_anyone_creates_and_deletes_tags_scope_and_delete_with_conversation(client, monkeypatch):
    from app import config

    b = conv("STORE_B", "C3")
    make_user("cs_tag", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_tag")
    # 所有账号都能在弹窗里新建、删除标记项；删除时所有会话上的这个标记一起去掉
    own = cs.post("/internal/message-tags", json={"name": "等配件到货", "color": "warning"}).json()
    assert own["created_by"] == "cs_tag"
    a = conv("STORE_A", "C4")
    cs.put(f"/internal/conversations/{a}/mark", json={"tag_id": own["id"], "note": "周五到"})
    assert cs.delete(f"/internal/message-tags/{own['id']}").json() == {"removed_marks": 1}
    assert cs.get(f"/internal/conversations/{a}").json()["conversation"]["marks"] == []
    assert all(t["id"] != own["id"] for t in cs.get("/internal/message-tags").json())
    tag_id = client.post("/internal/message-tags", json={"name": "等补发", "color": "danger"}).json()["id"]
    assert client.post("/internal/message-tags", json={"name": "等补发"}).status_code == 409
    assert cs.put(f"/internal/conversations/{b}/mark", json={"tag_id": tag_id}).status_code == 404  # 不是自己的店铺

    client.put(f"/internal/conversations/{b}/mark", json={"tag_id": tag_id})
    monkeypatch.setenv("TEST_DELETE_ENABLED", "true")
    config.get_settings.cache_clear()
    assert client.delete(f"/internal/conversations/{b}").status_code == 200  # 有标记的会话也能删（标记一起删）
    assert cs.patch(f"/internal/message-tags/{tag_id}", json={"is_active": False}).status_code == 403  # 改名、停用仍是管理员
    client.patch(f"/internal/message-tags/{tag_id}", json={"is_active": False})
    assert all(t["id"] != tag_id for t in cs.get("/internal/message-tags").json())


def test_filter_counts(client):
    from app.db import session_scope
    from app.models import Conversation

    fit = next(t for t in client.get("/internal/message-tags").json() if t["name"] == "查适配中")
    a, b, c = conv("STORE_A", "N1"), conv("STORE_A", "N2"), conv("STORE_B", "N3")
    client.put(f"/internal/conversations/{a}/mark", json={"tag_id": fit["id"]})
    client.put(f"/internal/conversations/{c}/mark", json={"tag_id": fit["id"]})
    with session_scope() as db:
        db.get(Conversation, b).aftersales_status = "PENDING"
    r = client.get("/internal/conversations/filter-counts").json()
    assert (r["all"], r["aftersales"], r["aftersales_done"], r["invoice"]) == (3, 1, 0, 0)
    assert r["tags"][str(fit["id"])] == 2
    r = client.get("/internal/conversations/filter-counts", params={"store_id": "STORE_A", "q": "N1"}).json()
    assert r["all"] == 1 and r["tags"] == {str(fit["id"]): 1} and r["aftersales"] == 0
