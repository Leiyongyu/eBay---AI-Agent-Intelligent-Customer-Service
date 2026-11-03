"""物流查询：谷仓出库单（只读）+ 17TRACK 轨迹缓存与额度控制 + 查包裹工具 + 风险规则。全部用模拟接口，不真实调用。"""
import json

import httpx
import pytest

from app.agent.schemas import RuntimeContext
from app.config import get_settings
from app.logistics import service
from app.logistics.goodcang import GoodcangClient, GoodcangError
from app.logistics.track17 import Track17Client, Track17Error, summarize

CTX = RuntimeContext(store_id="STORE_A", conversation_id=1, buyer_key="buyer_x", marketplace_id="EBAY_US",
                     item_id=None, job_id=1, run_generation=1)
GC_ROW = {"order_code": "G10645-261001-0099", "reference_no": "27-15204-56308", "platform": "EBAY",
          "order_status": "D", "shipping_method": "GC_PARCEL", "carrier_name": "YunExpress",
          "tracking_no": "YT2641021461539465", "warehouse_code": "USEA", "date_create": "2026-10-01 13:06:28",
          "date_shipping": "2026-10-01 21:29:41", "consignee_name": "Jane Doe", "consignee_address1": "1 High St",
          "consignee_phone": "0123", "consignee_email": "j@x.com"}


def track_accepted(number, status="InTransit", sub="InTransit_Arrival", events=None):
    events = events if events is not None else [
        {"time_iso": "2026-10-03T08:00:00-05:00", "location": "NEW YORK", "description": "Arrived at facility",
         "sub_status": "InTransit_Arrival"},
        {"time_iso": "2026-10-02T10:00:00+08:00", "location": "SHENZHEN", "description": "Picked up",
         "sub_status": "InTransit_PickedUp"}]
    return {"number": number, "carrier": 190008, "track_info": {
        "latest_status": {"status": status, "sub_status": sub},
        "latest_event": events[0] if events else None,
        "time_metrics": {"days_of_transit": 2, "estimated_delivery_date": {"from": "2026-10-08", "to": "2026-10-10"}},
        "tracking": {"providers": [{"provider": {"name": "YunExpress"}, "events": events}]}}}


# ---------------------------------------------------------------- 接口客户端
def test_goodcang_client_reads_order_and_drops_personal_info(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen.update(path=req.url.path, token=req.headers.get("app-token"), body=json.loads(req.content))
        ref = seen["body"]["order_code"]
        if ref == "27-15204-56308":  # 一票两箱；另带一条参考号不一致的（不能当成这一单）
            row = {**GC_ROW, "orderBoxInfo": [{"box_no": "1-1", "tracking_number": "YT2641021461539465"},
                                              {"box_no": "1-2", "tracking_number": "YT2641021461539466"}]}
            return httpx.Response(200, json={"ask": "Success", "message": "Success", "count": 2,
                                             "data": [row, {**GC_ROW, "reference_no": "OTHER"}]})
        if ref == "BAD":
            return httpx.Response(200, json={"ask": "Failure", "message": "签名错误"})
        return httpx.Response(200, json={"ask": "Success", "message": "Success", "count": 0, "data": []})

    s = get_settings().model_copy(update={"goodcang_app_token": "t", "goodcang_app_key": "k",
                                          "goodcang_min_request_interval_ms": 0})
    gc = GoodcangClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = gc.orders_by_reference(" 27-15204-56308 ")
    assert seen["path"].endswith("/order/get_order_list") and seen["token"] == "t"
    assert seen["body"]["code_type"] == "reference_no" and seen["body"]["order_code"] == "27-15204-56308"
    assert len(rows) == 1
    row = rows[0]
    assert (row["tracking_no"], row["carrier_name"], row["status_label"]) == ("YT2641021461539465", "YunExpress", "已发货")
    assert row["tracking_numbers"] == ["YT2641021461539465", "YT2641021461539466"]  # 每箱各自的跟踪号
    assert not any(k.startswith("consignee") for k in row)  # 收件人信息一律不取
    assert gc.orders_by_reference("NONE") == []
    with pytest.raises(GoodcangError, match="签名错误"):
        gc.orders_by_reference("BAD")


def test_track17_client_and_summary():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.url.path.rsplit("/", 1)[-1], req.headers.get("17token")))
        if req.url.path.endswith("/gettrackinfo"):
            return httpx.Response(200, json={"code": 0, "data": {"accepted": [track_accepted("YT1")], "rejected": []}})
        return httpx.Response(401)

    s = get_settings().model_copy(update={"track17_enabled": True, "track17_api_key": "key17"})
    c = Track17Client(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    t = summarize(c.track_info([{"number": "YT1"}])["YT1"])
    assert calls == [("gettrackinfo", "key17")]
    assert (t["status_label"], t["sub_status_label"], t["carrier"]) == ("运输途中", "已到达目的地国家", "YunExpress")
    assert t["latest_event"]["description"] == "Arrived at facility" and len(t["events"]) == 2
    assert t["estimated_delivery"] == {"from": "2026-10-08", "to": "2026-10-10", "source": None}
    with pytest.raises(Track17Error, match="拒绝访问"):
        c.quota()
    off = Track17Client(get_settings().model_copy(update={"track17_enabled": False}))
    with pytest.raises(Track17Error, match="TRACK17_ENABLED"):
        off.quota()


# ---------------------------------------------------------------- 查询服务：缓存与额度
class FakeTrack:
    def __init__(self, register=None, info=None):
        self.calls = []
        self.register_result = register or (lambda items: ([{"number": i["number"], "carrier": 190008} for i in items], []))
        self.info = info or (lambda n: track_accepted(n))

    def register(self, items):
        self.calls.append(("register", [i["number"] for i in items], items[0].get("carrier")))
        return self.register_result(items)

    def track_info(self, items):
        self.calls.append(("info", [i["number"] for i in items]))
        return {i["number"]: self.info(i["number"]) for i in items}


class FakeGoodcang:
    def __init__(self, row=GC_ROW):
        self.row = row

    def orders_by_reference(self, ref):
        return [{**{k: self.row.get(k) for k in ("order_code", "tracking_no", "carrier_name", "date_shipping",
                                                  "shipping_method", "warehouse_code")},
                 "status_label": "已发货", "tracking_numbers": [self.row["tracking_no"]]}]


@pytest.fixture()
def logistics_on(app_env, monkeypatch):
    s = get_settings()
    for k, v in {"track17_enabled": True, "track17_api_key": "k", "goodcang_app_token": "t", "goodcang_app_key": "k",
                 "track17_wait_seconds": 0, "track17_register_daily_limit": 2}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(service, "carrier_code", lambda name: {"YunExpress": 190008}.get(name))
    return s


def test_lookup_merges_goodcang_registers_once_and_caches(logistics_on):
    from app.db import session_scope

    track = FakeTrack()
    with session_scope() as db:
        r = service.lookup(db, "STORE_A", "27-15204-56308", [], goodcang=FakeGoodcang(), track=track)
    p = r["packages"][0]
    assert (p["source"], p["tracking_number"], p["carrier"]) == ("goodcang", "YT2641021461539465", "YunExpress")
    assert p["tracking"]["status"] == "InTransit" and "tracking_note" not in p
    # 承运商能对上就明确传代码，避免识别错白扣额度；注册时带订单号和发货日期
    assert track.calls[0] == ("register", ["YT2641021461539465"], 190008)

    # eBay 也有同一个物流号：合并成一个包裹；缓存时长内不再调用 17TRACK
    with session_scope() as db:
        r = service.lookup(db, "STORE_A", "27-15204-56308",
                           [{"order_id": "27-15204-56308", "package_id": "F1", "tracking_number": "yt2641021461539465 ",
                             "carrier": "Yun Express"}], goodcang=FakeGoodcang(), track=track)
    assert len(r["packages"]) == 1 and r["packages"][0]["source"] == "ebay" and r["packages"][0]["tracking"]
    assert len(track.calls) == 2  # 只有第一次的 register + info


def test_daily_limit_rejected_and_not_yet(logistics_on):
    from app.db import session_scope

    track = FakeTrack(info=lambda n: track_accepted(n, status="NotFound", sub="NotFound_Other", events=[]))
    pk = lambda n: {"order_id": "O1", "package_id": n, "tracking_number": n, "carrier": "YunExpress"}
    with session_scope() as db:
        r = service.lookup(db, "STORE_A", "O1", [pk("N1"), pk("N2"), pk("N3")], track=track)
    notes = [p.get("tracking_note") for p in r["packages"]]
    assert "稍后可用" in notes[0] and "稍后可用" in notes[1]  # 刚注册还没上网
    assert "已达上限（2 个）" in notes[2]  # 每日注册上限
    assert [c[0] for c in track.calls].count("register") == 2

    logistics_on.track17_register_daily_limit = 10
    bad = FakeTrack(register=lambda items: ([], [{"number": items[0]["number"],
                                                  "error": {"code": -18019903, "message": "Carrier cannot be detected."}}]))
    with session_scope() as db:
        r = service.lookup(db, "STORE_B", "O2", [pk("ZZ99")], track=bad)
        assert "无法跟踪" in r["packages"][0]["tracking_note"]
        r = service.lookup(db, "STORE_B", "O2", [pk("ZZ99")], track=bad)  # 被拒的不再重复注册
    assert [c[0] for c in bad.calls] == ["register"]


def test_package_tool_uses_goodcang_when_ebay_has_none(logistics_on, monkeypatch):
    from app.db import session_scope
    from app.tools.readonly import OrderIdArgs, get_package_status

    class Src:
        name = "fake"

        def find_orders(self, store_id, buyer_key):
            return [{"order_id": "27-15204-56308"}]

        def find_packages(self, store_id, order_id):
            return []

    monkeypatch.setattr(service, "GoodcangClient", lambda s: FakeGoodcang())
    monkeypatch.setattr(service, "Track17Client", lambda s: FakeTrack())
    with session_scope() as db:
        status, data, ref, _ = get_package_status(CTX, Src(), OrderIdArgs(order_id="27-15204-56308"), db=db)
    assert status == "FOUND" and ref == "package:G10645-261001-0099"
    assert (data["tracking_status"], data["latest_event"]) == ("运输途中", "Arrived at facility")
    assert data["warehouse"]["status_label"] == "已发货" and data["tracking"]["events"]
    assert "Jane" not in json.dumps(data)


def test_tracking_risk_reasons():
    from app.agent.nodes import _tracking_problems

    def state(t):
        return {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": {"tracking": t}}]}

    assert _tracking_problems(state({"status": "Exception", "sub_status": "Exception_Lost"}), "Where?") == [
        "TRACKING_EXCEPTION"]
    assert _tracking_problems(state({"status": "Delivered"}), "Das Paket ist nicht angekommen") == [
        "TRACKING_DELIVERED_NOT_RECEIVED"]
    assert _tracking_problems(state({"status": "Delivered"}), "Thanks, got it!") == []
    assert _tracking_problems(state({"status": "InTransit"}), "Where is my parcel?") == []
