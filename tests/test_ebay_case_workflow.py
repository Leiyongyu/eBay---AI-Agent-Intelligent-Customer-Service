"""售后管理在本系统里的处理：页签（待处理 / 处理中 / 已处理）、暂不处理原因、星标、标签、备注、认领、删除（隐藏）、
筛选，以及再从 eBay 拉取时不覆盖这些处理记录。"""
from test_ebay_cases import RET


def _seed():
    from app import ebay_cases as ec
    from app.db import session_scope

    rows = [ec.parse_return({**RET, "returnId": str(5300000 + i), "orderId": f"01-111-{i:03d}",
                             "buyerLoginName": f"buyer_{i}"}) for i in range(4)]
    rows[3] = ec.parse_return({**RET, "returnId": "5300003", "orderId": "01-111-003", "buyerLoginName": "buyer_3",
                               "state": "CLOSED", "status": "CLOSED"})
    with session_scope() as db:
        ec.save(db, "STORE_A", "RETURN", rows)
    return rows


def _ids(client, **params):
    return [r["external_id"] for r in client.get("/internal/ebay-cases", params={"kind": "RETURN", **params}).json()]


def test_views_batch_tags_note_and_resync(client):
    from app import ebay_cases as ec
    from app.db import session_scope

    rows = _seed()
    all_rows = client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()
    by_ext = {r["external_id"]: r["id"] for r in all_rows}
    a, b, c, closed = (by_ext[r["external_id"]] for r in rows)

    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "RETURN"}).json()
    assert vc["views"] == {"all": 4, "pending": 3, "processing": 0, "done": 1}

    # 处理中、暂不处理（带原因）、标为已处理
    assert client.post("/internal/ebay-cases/batch", json={"ids": [a], "action": "state", "state": "PROCESSING"}).status_code == 200
    client.post("/internal/ebay-cases/batch", json={"ids": [b], "action": "state", "state": "HOLD", "hold_reason": "等买家寄回"})
    client.post("/internal/ebay-cases/batch", json={"ids": [c], "action": "state", "state": "DONE"})
    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "RETURN"}).json()
    assert vc["views"] == {"all": 4, "pending": 0, "processing": 2, "done": 2}
    proc = {r["id"]: r for r in client.get("/internal/ebay-cases", params={"kind": "RETURN", "view": "processing"}).json()}
    assert set(proc) == {a, b} and proc[b]["hold_reason"] == "等买家寄回" and proc[a]["operator"] == "tester"
    assert client.get("/internal/ebay-cases/options", params={"kind": "RETURN"}).json()["hold_reasons"] == ["等买家寄回"]

    # 标签、星标、备注、认领
    tag = client.post("/internal/ebay-cases/tags", json={"kind": "RETURN", "name": "不退回", "color": "danger"}).json()
    assert client.post("/internal/ebay-cases/tags", json={"kind": "RETURN", "name": "不退回"}).status_code == 409
    client.post("/internal/ebay-cases/batch", json={"ids": [a, b], "action": "tag_add", "tag_id": tag["id"]})
    client.put(f"/internal/ebay-cases/{a}/star", json={"starred": True})
    note = client.put(f"/internal/ebay-cases/{a}/note", json={"note": "11.10到期 协商重发"}).json()
    assert note["note_by"] == "tester"
    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "RETURN"}).json()
    assert vc["starred"] == 1 and vc["tags"] == {str(tag["id"]): 2}
    assert set(_ids(client, tag_id=tag["id"])) == {"5300000", "5300001"}
    assert _ids(client, starred="true") == ["5300000"]
    assert set(_ids(client, operator="unassigned")) == {"5300001", "5300003"}

    # 搜索：多个编号、开头匹配、精确匹配
    assert set(_ids(client, q="5300000,5300002", q_field="external_id", match="exact")) == {"5300000", "5300002"}
    assert set(_ids(client, q="01-111-00", q_field="order_id", match="prefix")) == {"5300000", "5300001", "5300002", "5300003"}
    assert _ids(client, q="111-00", q_field="order_id", match="prefix") == []

    # 再从 eBay 拉一次：处理记录、标签、备注都还在
    with session_scope() as db:
        ec.save(db, "STORE_A", "RETURN", rows)
    row = next(r for r in client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json() if r["id"] == a)
    assert row["view"] == "processing" and row["starred"] and row["tag_ids"] == [tag["id"]] and row["note"].startswith("11.10")

    # 删除（只在本系统隐藏），再拉取也不会回来；删标签会从记录上去掉
    client.post("/internal/ebay-cases/batch", json={"ids": [c], "action": "hide"})
    with session_scope() as db:
        ec.save(db, "STORE_A", "RETURN", rows)
    assert "5300002" not in _ids(client)
    client.delete(f"/internal/ebay-cases/tags/{tag['id']}")
    assert all(not r["tag_ids"] for r in client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json())
    assert client.post("/internal/ebay-cases/batch", json={"ids": [a], "action": "state", "state": "BAD"}).status_code == 422


def test_extras_and_feedback_ratings(client):
    from app import ebay_cases as ec
    from app.db import session_scope
    import xml.etree.ElementTree as ET
    from test_ebay_cases import FEEDBACK_XML

    root = ET.fromstring(FEEDBACK_XML)
    ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
    with session_scope() as db:
        ec.save(db, "STORE_A", "FEEDBACK", [ec.parse_feedback(el, ns) for el in root.findall("e:FeedbackDetailArray/e:FeedbackDetail", ns)])
    vc = client.get("/internal/ebay-cases/view-counts", params={"kind": "FEEDBACK"}).json()
    assert vc["ratings"] == {"negative": 1, "neutral": 0, "positive": 0}
    assert len(client.get("/internal/ebay-cases", params={"kind": "FEEDBACK", "rating": "negative"}).json()) == 1

    cid = client.get("/internal/ebay-cases", params={"kind": "FEEDBACK"}).json()[0]["id"]
    ex = client.get(f"/internal/ebay-cases/{cid}/extras")
    assert ex.status_code == 200 and "picture_url" in ex.json() and ex.json()["title"] == "Turbo"


def test_unknown_api_path_returns_json_404(client):
    """不存在的接口返回 JSON 404，不回首页 HTML（前端才能提示“请重启后端”）。"""
    r = client.get("/internal/ebay-cases-no-such/thing")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
