"""消息模板：系统预定义（启动写入，只有管理员能改）、企业模板、个人模板（只有自己看得到）、筛选、另存为、标签管理、图片附件。"""
import base64

from fastapi.testclient import TestClient

from conftest import login, make_user

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode()


def test_seed_scopes_permissions_and_filters(client):
    d = client.get("/internal/message-templates").json()
    assert d["counts"]["system"] >= 5 and all(t["scope"] == "SYSTEM" for t in d["items"])
    sys_t = d["items"][0]
    assert "{{buyer}}" in sys_t["content"] and sys_t["editable"]  # 测试账号是管理员

    make_user("cs_tpl", "CS")
    cs = TestClient(client.app)
    login(cs, "cs_tpl")
    # 客服不能改系统预定义，可以另存为个人模板
    body = {"scope": "SYSTEM", "usage": "MESSAGE", "title": sys_t["title"], "content": sys_t["content"]}
    assert cs.put(f"/internal/message-templates/{sys_t['id']}", json=body).status_code == 403
    assert cs.post("/internal/message-templates", json=body).status_code == 403
    mine = cs.post("/internal/message-templates", json={**body, "scope": "PERSONAL", "title": "我的VIN模板",
                                                         "tags": ["适配", "适配", " VIN "]}).json()
    assert mine["mine"] and mine["tags"] == ["适配", "VIN"]
    company = cs.post("/internal/message-templates", json={**body, "scope": "COMPANY", "title": "企业：催评",
                                                            "usage": "FEEDBACK"}).json()

    # 个人模板只有自己看得到；企业模板大家都看得到，但只有创建人或管理员能改
    admin_titles = [t["title"] for t in client.get("/internal/message-templates").json()["items"]]
    assert "我的VIN模板" not in admin_titles and "企业：催评" in admin_titles
    make_user("cs_tpl2", "CS")
    other = TestClient(client.app)
    login(other, "cs_tpl2")
    assert other.put(f"/internal/message-templates/{company['id']}", json={**body, "scope": "COMPANY"}).status_code == 403
    assert other.get("/internal/message-templates", params={"scope": "mine"}).json()["items"] == []

    # 页签数量与筛选
    r = cs.get("/internal/message-templates", params={"scope": "mine"}).json()
    assert [t["title"] for t in r["items"]] == ["我的VIN模板"] and r["counts"]["mine"] == 1 and r["counts"]["company"] == 1
    assert [t["title"] for t in cs.get("/internal/message-templates", params={"usage": "FEEDBACK", "scope": "company"}).json()["items"]] == ["企业：催评"]
    assert cs.get("/internal/message-templates", params={"q": "vin模板"}).json()["counts"]["all"] == 1
    assert cs.get("/internal/message-templates", params={"q": "{{buyer}}", "q_field": "content"}).json()["counts"]["all"] >= 3
    assert [t["title"] for t in cs.get("/internal/message-templates", params={"tag": "VIN"}).json()["items"]] == ["我的VIN模板"]

    # 星标、标签改名 / 删除、修改、删除
    cs.put(f"/internal/message-templates/{mine['id']}/star", json={"starred": True})
    assert cs.get("/internal/message-templates", params={"starred": "true"}).json()["counts"]["all"] == 1
    assert cs.post("/internal/message-templates/tags/rename", json={"old": "VIN", "new": "车架号"}).json()["updated"] == 1
    assert "车架号" in cs.get("/internal/message-templates/meta").json()["tags"]
    cs.post("/internal/message-templates/tags/delete", json={"name": "车架号"})
    upd = cs.put(f"/internal/message-templates/{mine['id']}", json={**body, "scope": "PERSONAL", "title": "改过的标题"}).json()
    assert upd["title"] == "改过的标题" and upd["updated_by"] == "cs_tpl"
    assert other.delete(f"/internal/message-templates/{mine['id']}").status_code == 404
    assert cs.delete(f"/internal/message-templates/{mine['id']}").json()["ok"]

    meta = cs.get("/internal/message-templates/meta").json()
    assert {"label": "订单编号", "key": "orderNumber"} in meta["placeholders"] and not meta["can_edit_system"]


def test_image_attachment_upload_and_validation(client):
    up = client.post("/internal/message-templates/files", json={"name": "vin.png", "data": f"data:image/png;base64,{PNG}"}).json()
    assert up["file"].endswith(".png")
    assert client.get(up["url"]).status_code == 200
    assert client.post("/internal/message-templates/files", json={"data": base64.b64encode(b"hello world!").decode()}).status_code == 422
    assert client.get("/internal/message-templates/files/..%2F..%2Fapp.db").status_code == 404

    t = client.post("/internal/message-templates", json={"scope": "COMPANY", "title": "带图", "content": "see photo",
                                                         "attachments": [{"file": up["file"], "name": "vin.png"}]}).json()
    assert t["attachments"][0]["url"] == up["url"]
    bad = client.post("/internal/message-templates", json={"scope": "COMPANY", "title": "x", "content": "y",
                                                           "attachments": [{"file": "nope.png"}]})
    assert bad.status_code == 422


def test_render_fills_placeholders_from_conversation(client):
    from test_customer_panel import _conv

    cid = _conv("STORE_A", "TPL1", "buyer_us_001", "110000000001")
    t = client.post("/internal/message-templates", json={
        "scope": "PERSONAL", "title": "物流", "content":
            "Hi {{buyer}}, order {{orderNumber}} item {{itemNumber}} tracking {{trackNumber}} via {{shippingCompany}}. "
            "Unknown {{itemAttribute}} and {{itemAttribute}}. {{sellerName}}"}).json()
    r = client.post(f"/internal/message-templates/{t['id']}/render", json={"conversation_id": cid}).json()
    assert "Hi buyer_us_001, order 26-10001-US item 110000000001 tracking 9400100000000000010001 via USPS." in r["text"]
    assert "{{itemAttribute}}" in r["text"] and r["missing"] == [{"key": "itemAttribute", "label": "Listing属性"}]
    assert "{{sellerName}}" not in r["text"]
    assert client.post(f"/internal/message-templates/{t['id']}/render", json={"conversation_id": 999999}).status_code == 404
