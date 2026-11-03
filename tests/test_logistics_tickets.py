"""物流第二部分：断更天数判断、工具不给送达日期、补发转人工、谷仓查件单同步与回复建议。全部用模拟接口。"""
import json
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.logistics import service
from test_logistics import CTX, FakeGoodcang, FakeTrack, logistics_on  # noqa: F401  （fixture 复用）


# ---------------------------------------------------------------- 断更天数判断（只看轨迹多少天没更新）
def test_assess_days_without_update():
    from app.logistics.assess import assess

    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    s = get_settings().model_copy(update={"logistics_stall_days": 4, "logistics_reship_days": 7})

    def pkg(status="InTransit", last="2026-11-08T12:00:00Z"):
        events = [{"time": last, "time_utc": last}] if last else []
        return {"tracking_number": "N1", "tracking": {"status": status, "events": events}}

    def stage(p, on=True):
        return assess(p, tracking_enabled=on, now=now, settings=s)

    assert stage(pkg())["stage"] == "OK" and stage(pkg())["days_without_update"] == 2.0
    stalled = stage(pkg(last="2026-11-05T12:00:00Z"))
    assert (stalled["stage"], stalled["reason_code"]) == ("STALLED", "TRACKING_STALLED")
    assert stage(pkg(last="2026-11-02T12:00:00Z"))["stage"] == "RESHIP_REVIEW"
    assert stage(pkg(status="Delivered", last="2026-10-01T00:00:00Z"))["stage"] == "DELIVERED"
    assert stage(pkg(status="Exception"))["needs_human"] is True
    assert stage(pkg(status="NotFound", last=None))["reason_code"] == "TRACKING_NOT_FOUND"
    assert stage({"tracking_number": None})["stage"] == "NO_TRACKING"
    assert stage(pkg(), on=False)["stage"] == "UNKNOWN"
    # 没有轨迹节点时用谷仓揽收时间（东八区）：11-05 20:00 北京 = 11-05 12:00 UTC → 5 天
    a = stage({"tracking_number": "N1", "tracking": {"status": "InTransit", "events": []},
               "warehouse": {"ascan_time": "2026-11-05 20:00:00"}})
    assert (a["stage"], a["days_without_update"]) == ("STALLED", 5.0)


def test_tool_hides_delivery_dates_and_gives_full_events(logistics_on, monkeypatch):  # noqa: F811
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
        _, data, _, _ = get_package_status(CTX, Src(), OrderIdArgs(order_id="27-15204-56308"), db=db)
    text = json.dumps(data)
    assert "estimated_delivery" not in text  # 不给模型任何送达日期
    assert len(data["tracking"]["events"]) == 2 and data["assessment"]["stage"]

    empty_gc = type("G", (), {"orders_by_reference": lambda self, r: []})
    monkeypatch.setattr(service, "GoodcangClient", lambda s: empty_gc())
    with session_scope() as db:
        status, data, _, msg = get_package_status(CTX, Src(), OrderIdArgs(order_id="27-15204-56308"), db=db)
    assert status == "NOT_FOUND" and data["assessment"]["reason_code"] == "TRACKING_NOT_FOUND" and "人工" in msg


def test_reship_and_assessment_reasons():
    from app.agent.nodes import _tracking_problems

    def obs(a, status="FOUND"):
        return {"observations": [{"tool_name": "get_package_status", "status": status,
                                  "data": {"assessment": a, "tracking": {"status": "InTransit"}}}]}

    assert _tracking_problems(obs({"reason_code": "TRACKING_RESHIP_REVIEW"}), "Where?") == ["TRACKING_RESHIP_REVIEW"]
    assert _tracking_problems({"observations": []}, "Bitte schicken Sie Ersatz") == ["RESHIP_REQUESTED"]
    assert _tracking_problems({"observations": []}, "Can you send a replacement?") == ["RESHIP_REQUESTED"]
    assert _tracking_problems(obs({"reason_code": "TRACKING_NOT_FOUND"}, "NOT_FOUND"), "Wo ist mein Paket?") == [
        "TRACKING_NOT_FOUND"]
    assert _tracking_problems(obs({"stage": "OK"}), "Where is my parcel?") == []


def test_logistics_human_marks_conversation(app_env):
    from app.agent.nodes import _mark_logistics_aftersales
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.models import Conversation

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-M1", "buyer_m", None, "EBAY_DE", None)
        job = type("J", (), {"conversation_id": conv.id, "id": 1})()
        state = {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": {
            "carrier": "DHL Paket", "tracking_number": "003404", "assessment": {
                "needs_human": True, "stage_label": "轨迹长时间未更新，需判断补发", "days_without_update": 8.0}}}]}
        assert _mark_logistics_aftersales(db, job, state, ["TRACKING_RESHIP_REVIEW", "AUTO_SEND_DISABLED"]) == "物流超时需判断补发"
        c = db.get(Conversation, conv.id)
        assert (c.aftersales_status, c.aftersales_marked_by) == ("PENDING", "AI（物流规则）")
        assert "8.0 天未更新" in c.aftersales_note
        assert _mark_logistics_aftersales(db, job, state, ["TRACKING_STALLED"]) is None  # 已在待售后：不重复改


# ---------------------------------------------------------------- 查件单同步
class FakeTickets:
    def __init__(self, rows):
        self.rows = rows

    def ticket_list(self, time_start, time_end, page=1, page_size=200):
        return (self.rows if page == 1 else []), len(self.rows)


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def structured(self, messages, schema):
        from app.providers.chat import StructuredResult

        self.calls += 1
        text = messages[-1].content
        assert "<ticket>" in text and "轨迹久未更新" in text and "Where is my parcel?" in text
        parsed = schema(language="de", reply_text="Wir haben beim Paketdienst nachgefragt.", reply_zh="我们已向承运商核实。",
                        needs_human=False, reason="")
        return StructuredResult(parsed, "", None, {"total_tokens": 10})


def test_ticket_sync_locates_suggests_and_marks(app_env, monkeypatch, tmp_path):
    from sqlalchemy import select

    from app.aftersales_mail.service import Located
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.logistics import tickets
    from app.logistics.assess import CN
    from app.models import AftersalesFile, Conversation, LogisticsTicket

    s = get_settings()
    monkeypatch.setattr(s, "aftersales_files_dir", str(tmp_path / "files"))  # 附件写临时目录，不碰 data/
    monkeypatch.setattr(s, "aftersales_virus_scan", False)
    recent = (datetime.now(CN) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {"io_code": "CJ1", "reference_no": "09-15357-84924", "tracking_no": "GN1", "it_type_name": "断更",
         "io_status": 7, "io_status_text": "已回复待完结", "customer_service_reply": "您好，轨迹久未更新，未收到请考虑包裹丢失",
         "add_time": recent, "reply_time": recent,
         "replay_attachment_file_list": [{"name": "POD.pdf", "url": "https://files.example/pod.pdf"}]},
        {"io_code": "CJ2", "reference_no": "AMZ-123", "tracking_no": "X1", "it_type_name": "申请POD",
         "io_status": 7, "io_status_text": "已回复待完结", "customer_service_reply": "已提供POD", "add_time": recent},
        {"io_code": "CJ3", "reference_no": "09-15357-84924", "tracking_no": "GN1", "it_type_name": "断更",
         "io_status": 8, "io_status_text": "已完结", "customer_service_reply": "已恢复", "add_time": "2026-05-01 10:00:00",
         "reply_time": "2026-05-02 10:00:00"},
    ]
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-T1", "buyer_t", None, "EBAY_GB", None)
        _append_message(db, conv, "M1", "BUYER", "IN", "Where is my parcel?", None)
        conv_id = conv.id
    located = []

    def loc(db, ref):
        located.append(ref)
        return Located(ref, "STORE_A", "buyer_t", conv_id, "order_api")

    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"%PDF-1.4 test", headers={"content-type": "application/pdf"})))
    prov = FakeProvider()
    with session_scope() as db:
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert (stats["tickets"], stats["new"], stats["suggested"], stats["needs_human"]) == (3, 3, 1, 1)
    assert located == ["09-15357-84924"]  # 只定位 eBay 订单号；同一订单定位一次后记下
    with session_scope() as db:
        t1 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJ1"))
        assert t1.suggestion.startswith("Wir haben") and t1.needs_human  # 回复提到“丢失”：规则判定需人工
        f = db.get(AftersalesFile, t1.attachments[0]["file_id"])
        assert (f.source, f.status, f.order_id) == ("GOODCANG_TICKET", "STORED", "09-15357-84924")
        assert db.scalar(select(LogisticsTicket.suggestion).where(LogisticsTicket.io_code == "CJ3")) is None  # 旧回复不起草
        c = db.get(Conversation, conv_id)
        assert (c.aftersales_status, c.aftersales_type) == ("PENDING", "物流异常/未收到")
    with session_scope() as db:  # 再同步一次：不重复起草、不重复定位
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert stats["new"] == 0 and prov.calls == 1 and located == ["09-15357-84924"]


def test_ticket_endpoints(client):
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.models import LogisticsTicket

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-T2", "buyer_t2", None, "EBAY_GB", None)
        db.add(LogisticsTicket(io_code="CJ9", reference_no="01-00000-00000", store_id="STORE_A",
                               conversation_id=conv.id, type_name="断更", io_status_text="已回复待完结", reply="已恢复",
                               suggestion="Hello"))
        conv_id = conv.id
    rows = client.get(f"/internal/conversations/{conv_id}/logistics-tickets").json()
    assert [(r["io_code"], r["suggestion"], r["handled_at"]) for r in rows] == [("CJ9", "Hello", None)]
    r = client.post(f"/internal/logistics-tickets/{rows[0]['id']}/handled").json()
    assert r["handled_at"] and r["handled_by"]


# ---------------------------------------------------------------- 查件单草稿、补发库存
class FakeGC:
    """谷仓模拟：查件类型、保存草稿、出库单、库存。"""

    def __init__(self, sellable=15):
        self.created, self.sellable = [], sellable

    def ticket_types(self, sm_code=None):
        return [{"value": 1, "name": "妥投未收到"}, {"value": 2, "name": "断更"}, {"value": 13, "name": "留言"}]

    def create_ticket_draft(self, **kw):
        self.created.append(kw)
        return f"CJTEST{len(self.created)}"

    def orders_by_reference(self, ref):
        return [{"order_code": "G1-0001", "warehouse_code": "DE", "items": [{"sku": "JMH-20487-0226", "quantity": 1}]}]

    def inventory(self, skus):
        return [{"sku": "JMH-20487-0226", "warehouse_code": "DE", "warehouse_desc": "德国区", "sellable": self.sellable},
                {"sku": "JMH-20487-0226", "warehouse_code": "UK", "warehouse_desc": "英国区", "sellable": 3}]


def test_warehouse_sku():
    from app.logistics.followup import warehouse_sku

    assert warehouse_sku("BWM-50016-0101") == "JMH-50016-0101"
    assert warehouse_sku("JMH-20487-0226") == "JMH-20487-0226"
    assert warehouse_sku("FRD-70361-4-0734") == "JMH-70361-4-0734"
    assert warehouse_sku("ZMY-DE-BMW-30020-2-0018") == "JMH-30020-2-0018"
    assert warehouse_sku("MCD-20487-0226-AMZUS3") == "JMH-20487-0226"
    assert warehouse_sku("BMW-30020-2-0018-RXY") == "JMH-30020-2-0018-RXY"
    assert warehouse_sku("") is None


def test_reship_stock_decides_by_origin_warehouse():
    from app.logistics.followup import reship_stock

    r = reship_stock(FakeGC(), "03-15386-67153")
    assert r["all_in_stock"] and "可以补发" in r["summary"] and "英国区 3" in r["summary"]
    r = reship_stock(FakeGC(sellable=0), "03-15386-67153")
    assert not r["all_in_stock"] and "只能协商退款" in r["summary"]


def test_create_ticket_draft_saves_only(monkeypatch):
    from app.logistics.goodcang import GoodcangClient

    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"code": 0, "message": "Success", "data": {"io_code": "CJ1"}})

    s = get_settings().model_copy(update={"goodcang_app_token": "t", "goodcang_app_key": "k",
                                          "goodcang_min_request_interval_ms": 0})
    c = GoodcangClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.create_ticket_draft(order_code="G1", reference_no="03-1", tracking_no="N1", it_type_id=2,
                                 io_desc="轨迹已5天未更新，请核实", emails=["a@example.com"]) == "CJ1"
    assert seen["path"].endswith("/assistant/create_logistic_ticket")
    assert seen["body"]["save_type"] == 1 and seen["body"]["it_type_id"] == 2  # 只保存，不提交


def test_after_reply_drafts_ticket_once_and_notes_stock(app_env, monkeypatch):
    from sqlalchemy import select

    from app.agent.nodes import _mark_logistics_aftersales
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.logistics import followup
    from app.models import Conversation, LogisticsTicket
    from app.tools.readonly import _tickets_for

    s = get_settings()
    for k, v in {"logistics_ticket_auto_draft": True, "logistics_ticket_notify_emails": "a@example.com",
                 "goodcang_app_token": "t", "goodcang_app_key": "k"}.items():
        monkeypatch.setattr(s, k, v)
    pkg = {"order_id": "03-15386-67153", "carrier": "DHL Paket", "tracking_number": "003404",
           "warehouse": {"order_code": "G1-0001", "shipping_method": "DEDHL", "warehouse_code": "DE"},
           "tracking": {"status": "InTransit", "events": [{"time": "2026-10-25 08:00", "location": "Hamburg",
                                                           "description": "In Zustellung"}]},
           "assessment": {"stage": "RESHIP_REVIEW", "reason_code": "TRACKING_RESHIP_REVIEW", "needs_human": True,
                          "stage_label": "轨迹长时间未更新，需判断补发", "days_without_update": 8.0}}
    state = {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg}]}
    reasons = ["TRACKING_RESHIP_REVIEW"]
    gc = FakeGC()
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-D1", "buyer_d", None, "EBAY_DE", None)
        job = type("J", (), {"conversation_id": conv.id, "id": 1})()
        _mark_logistics_aftersales(db, job, state, reasons)
        out = followup.after_reply(db, job, conv, state, reasons, client=gc)
        conv_id = conv.id
    assert out["tickets"][0]["io_code"] == "CJTEST1" and out["tickets"][0]["type"] == "断更"
    assert "8天未更新" in gc.created[0]["io_desc"] and gc.created[0]["emails"] == ["a@example.com"]
    assert out["stock"]["all_in_stock"]
    with session_scope() as db:
        t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJTEST1"))
        assert (t.origin, t.conversation_id, t.store_id) == ("AI_DRAFT", conv_id, "STORE_A")
        assert _tickets_for(db, "03-15386-67153", ["003404"])[0]["submitted"] is False  # 草稿不能说已联系承运商
        note = db.get(Conversation, conv_id).aftersales_note
        assert "可以补发" in note and "CJTEST1" in note
        out = followup.after_reply(db, job, db.get(Conversation, conv_id), state, reasons, client=gc)
    assert out["tickets"][0]["skipped"] == "已有未完结的查件单" and len(gc.created) == 1

    monkeypatch.setattr(s, "logistics_ticket_auto_draft", False)  # 关掉自动建单：只查库存
    with session_scope() as db:
        out = followup.after_reply(db, job, db.get(Conversation, conv_id), state, reasons, client=gc)
    assert "tickets" not in out and out["stock"]
