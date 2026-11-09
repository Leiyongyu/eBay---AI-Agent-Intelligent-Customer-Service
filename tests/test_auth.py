"""登录与权限：未登录 401；角色限制；店铺范围；客服转售后、售后处理；账号管理只有管理员。"""
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD, login, make_user


def conv(store, key):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        c = get_or_create_conversation(db, store, key, "buyer_" + key, None, "EBAY_US", "simulator")
        _append_message(db, c, f"SIM-IN-{key}", "BUYER", "IN", "Where is my refund?", None)
        db.flush()
        return c.id


def as_user(client, username, role, stores=(), all_stores=False):
    make_user(username, role, stores=stores, all_stores=all_stores)
    c = TestClient(client.app)
    login(c, username)
    return c


def test_login_logout_and_lockout(client):
    anon = TestClient(client.app)
    assert anon.get("/internal/conversations").status_code == 401
    assert anon.get("/api/health").status_code == 200  # 健康检查公开
    assert anon.post("/api/auth/login", json={"username": "tester", "password": "wrong"}).status_code == 401
    me = login(anon, "TESTER")  # 登录名不区分大小写
    assert me["role"] == "ADMIN" and me["all_stores"]
    assert anon.get("/internal/conversations").status_code == 200
    anon.post("/api/auth/logout")
    assert anon.get("/internal/conversations").status_code == 401
    for _ in range(5):
        anon.post("/api/auth/login", json={"username": "tester", "password": "nope"})
    assert anon.post("/api/auth/login", json={"username": "tester", "password": TEST_PASSWORD}).status_code == 429


def test_admin_creates_account_and_first_login_must_change_password(client):
    r = client.post("/api/admin/users", json={"username": "cs01", "display_name": "客服小王", "role": "CS",
                                              "password": "Init-pass-1", "store_ids": ["STORE_A"]})
    assert r.status_code == 200 and r.json()["store_ids"] == ["STORE_A"] and r.json()["must_change_password"]
    cs = TestClient(client.app)
    assert cs.post("/api/auth/login", json={"username": "cs01", "password": "Init-pass-1"}).status_code == 200
    assert cs.get("/internal/conversations").status_code == 403  # 先改初始密码
    assert cs.post("/api/auth/password", json={"old_password": "Init-pass-1", "new_password": "short"}).status_code == 422
    assert cs.post("/api/auth/password", json={"old_password": "Init-pass-1", "new_password": "New-pass-123"}).status_code == 200
    assert cs.get("/internal/conversations").status_code == 200
    assert cs.get("/api/admin/users").status_code == 403  # 只有管理员管账号
    assert cs.get("/internal/maintenance/retention").status_code == 403
    uid = r.json()["id"]
    assert client.patch(f"/api/admin/users/{uid}", json={"is_active": False}).status_code == 200
    assert cs.get("/internal/conversations").status_code == 401  # 停用后立即失效


def test_store_scope(client):
    a, b = conv("STORE_A", "A1"), conv("STORE_B", "B1")
    cs = as_user(client, "cs_a", "CS", stores=["STORE_A"])
    ids = [i["id"] for i in cs.get("/internal/conversations", params={"group": "all"}).json()]
    assert a in ids and b not in ids
    assert cs.get(f"/internal/conversations/{b}").status_code == 404  # 别的店铺当作不存在
    assert {s["id"] for s in cs.get("/internal/stores").json()} == {"STORE_A"}
    assert cs.post(f"/internal/conversations/{b}/claim", json={"action": "claim"}).status_code == 404


def test_cs_marks_aftersales_and_aftersales_handles(client):
    cid = conv("STORE_A", "R1")
    cs = as_user(client, "cs_b", "CS", stores=["STORE_A"])
    af = as_user(client, "af_b", "AFTERSALES", stores=["STORE_A"])
    assert cs.post(f"/internal/conversations/{cid}/aftersales", json={"action": "mark", "type": "乱写"}).status_code == 422
    r = cs.post(f"/internal/conversations/{cid}/aftersales", json={"action": "mark", "type": "退款", "note": "买家要退款"})
    assert r.json()["status"] == "PENDING" and r.json()["marked_by"] == "cs_b"
    assert af.post(f"/internal/conversations/{cid}/aftersales", json={"action": "mark", "type": "退货"}).status_code == 403
    queue = af.get("/internal/conversations", params={"group": "aftersales"}).json()
    assert [i["id"] for i in queue] == [cid] and queue[0]["aftersales"]["type"] == "退款"
    assert cs.post(f"/internal/conversations/{cid}/aftersales", json={"action": "start"}).status_code == 403
    assert af.post(f"/internal/conversations/{cid}/aftersales", json={"action": "start"}).json()["handled_by"] == "af_b"
    assert cs.post(f"/internal/conversations/{cid}/aftersales", json={"action": "unmark"}).status_code == 409
    done = af.post(f"/internal/conversations/{cid}/aftersales", json={"action": "done", "note": "已退款"}).json()
    assert done["status"] == "DONE" and "已退款" in done["note"]
    assert af.get("/internal/conversations", params={"group": "aftersales"}).json() == []


def test_random_username_and_delete_account(client):
    """新建账号：登录名系统随机生成（字母开头、字母 + 数字），密码管理员自己设；删除账号连带登录会话，不能删自己和最后一个管理员。"""
    import re

    name = client.get("/api/admin/users/new-username").json()["username"]
    assert re.fullmatch(r"[a-z][a-z0-9]{7}", name) and re.search(r"\d", name)
    r = client.post("/api/admin/users", json={"username": name, "display_name": "客服小李", "role": "CS",
                                               "password": TEST_PASSWORD, "store_ids": ["STORE_A"]})
    assert r.status_code == 200 and r.json()["username"] == name
    auto = client.post("/api/admin/users", json={"display_name": "客服小张", "role": "CS", "password": TEST_PASSWORD})
    assert re.fullmatch(r"[a-z][a-z0-9]{7}", auto.json()["username"])  # 不传就当场生成

    cs = TestClient(client.app)
    login(cs, name)
    assert cs.get("/api/auth/me").status_code == 200
    assert cs.delete(f"/api/admin/users/{auto.json()['id']}").status_code == 403  # 只有管理员能删
    assert client.delete(f"/api/admin/users/{r.json()['id']}").json() == {"deleted": r.json()["id"]}
    assert cs.get("/api/auth/me").status_code == 401  # 已登录的立即失效
    assert all(u["username"] != name for u in client.get("/api/admin/users").json())

    me = client.get("/api/auth/me").json()["id"]
    assert client.delete(f"/api/admin/users/{me}").status_code == 409  # 不能删自己
