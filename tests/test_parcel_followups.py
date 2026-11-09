"""异常包裹跟进表：每人只看、只改自己的，管理员看全部；从会话新建时预填订单、SKU、跟踪号、查件单号；完结 / 重开；校验。"""
from fastapi.testclient import TestClient

from conftest import login, make_user
from test_pipeline import send_buyer


def _cs(client, name, stores=("STORE_A",)):
    make_user(name, "CS", stores=list(stores))
    c = TestClient(client.app)
    login(c, name)
    return c


def test_owner_only_and_admin_sees_all(client):
    a, b = _cs(client, "cs_a"), _cs(client, "cs_b")
    r = a.post("/internal/parcel-followups", json={"store_id": "STORE_A", "buyer_key": "baron068", "order_id": "26-10001-US",
                                                    "reason": "断更", "reason_note": "4.30断更", "carrier": "DHL",
                                                    "tracking_no": "00340434756793385199", "action": "重发",
                                                    "amount": 7.55, "currency": "eur"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["owner_name"], row["status"], row["currency"], row["occurred_on"] is not None) == ("cs_a", "OPEN", "EUR", True)
    b.post("/internal/parcel-followups", json={"store_id": "STORE_A", "buyer_key": "cornkits", "reason": "妥投未收到"})

    assert [x["buyer_key"] for x in a.get("/internal/parcel-followups").json()["items"]] == ["baron068"]  # 只看自己的
    assert b.patch(f"/internal/parcel-followups/{row['id']}", json={"result": "x"}).status_code == 404  # 改不了别人的
    assert b.delete(f"/internal/parcel-followups/{row['id']}").status_code == 404
    admin = client.get("/internal/parcel-followups").json()
    assert sorted(x["buyer_key"] for x in admin["items"]) == ["baron068", "cornkits"] and admin["is_admin"]
    owner_a = next(o["id"] for o in admin["owners"] if o["name"] == "cs_a")
    assert [x["buyer_key"] for x in client.get("/internal/parcel-followups", params={"owner_id": owner_a}).json()["items"]] == ["baron068"]
    assert "owners" not in a.get("/internal/parcel-followups").json()

    # 完结 → 进“已完结”，重开清掉完结时间；搜索跟踪号
    done = a.patch(f"/internal/parcel-followups/{row['id']}", json={"status": "DONE", "result": "已送达",
                                                                    "reship_tracking_no": "340434756793605000"}).json()
    assert done["status"] == "DONE" and done["closed_at"] and done["updated_by"] == "cs_a"
    lst = a.get("/internal/parcel-followups").json()
    assert lst["items"] == [] and lst["counts"] == {"OPEN": 0, "DONE": 1}
    found = a.get("/internal/parcel-followups", params={"status": "", "q": "340434756793605"}).json()["items"]
    assert [x["id"] for x in found] == [row["id"]]
    reopened = a.patch(f"/internal/parcel-followups/{row['id']}", json={"status": "OPEN"}).json()
    assert reopened["status"] == "OPEN" and reopened["closed_at"] is None
    assert client.delete(f"/internal/parcel-followups/{row['id']}").json() == {"removed": row["id"]}  # 管理员能删


def test_validation_and_store_scope(client):
    a = _cs(client, "cs_v")
    assert a.post("/internal/parcel-followups", json={"buyer_key": "x"}).status_code == 422  # 原因必填
    assert a.post("/internal/parcel-followups", json={"reason": "随便"}).status_code == 422
    assert a.post("/internal/parcel-followups", json={"reason": "断更", "occurred_on": "4.30"}).status_code == 422
    assert a.post("/internal/parcel-followups", json={"reason": "断更", "action": "删库"}).status_code == 422
    assert a.post("/internal/parcel-followups", json={"reason": "断更", "store_id": "STORE_B"}).status_code == 404  # 不是自己的店铺


def test_prefill_from_conversation(client):
    from app.db import session_scope
    from app.models import EbayOrderLine, LogisticsTicket, ShipNotice

    cid = send_buyer(client, "Paket kam nicht an", buyer="buyer_us_001")["conversation_id"]
    with session_scope() as db:
        db.add(EbayOrderLine(store_id="STORE_A", order_id="27-30003-US", line_item_id="L1", line_key="110000000001-1",
                             item_id="110000000001", buyer="buyer_us_001", sku="DAS-10760-0557"))
        db.add(ShipNotice(store_id="STORE_A", order_id="27-30003-US", ship_date="2026-11-01", language="de", text="x",
                          packages=[{"carrier": "DHL", "tracking_no": "00340434756793385199", "url": "https://www.dhl.de/"}]))
        db.add(LogisticsTicket(io_code="CJ202605060892", reference_no="27-30003-US", tracking_no="00340434756793385199",
                               type_name="断更", conversation_id=cid, store_id="STORE_A"))
    p = client.get("/internal/parcel-followups/prefill", params={"conversation_id": cid}).json()
    assert (p["store_id"], p["buyer_key"], p["order_id"], p["sku"]) == ("STORE_A", "buyer_us_001", "27-30003-US", "DAS-10760-0557")
    assert (p["carrier"], p["tracking_no"], p["ticket_no"], p["reason"], p["existing"]) == (
        "DHL", "00340434756793385199", "CJ202605060892", "断更", 0)
    r = client.post("/internal/parcel-followups", json={**{k: v for k, v in p.items() if k != "existing"}}).json()
    assert r["conversation_id"] == cid
    assert client.get("/internal/parcel-followups/prefill", params={"conversation_id": cid}).json()["existing"] == 1
    assert [x["id"] for x in client.get("/internal/parcel-followups", params={"conversation_id": cid}).json()["items"]] == [r["id"]]
