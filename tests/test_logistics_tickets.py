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
    # 到了取件柜 / ParcelShop：等买家去取，轨迹不再更新也不算断更（CJ202611034689 误判）
    pickup = stage(pkg(status="AvailableForPickup", last="2026-10-30T10:03:42+00:00"))
    assert (pickup["stage"], pickup["needs_human"], pickup["reason_code"]) == ("PICKUP", False, None)
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
        assert _mark_logistics_aftersales(db, job, state, ["TRACKING_RESHIP_REVIEW", "AUTO_SEND_DISABLED"]) == "物流问题需判断补发"
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
        assert (c.aftersales_status, c.aftersales_type) == ("PENDING", "物流问题需判断补发")  # 久未更新、丢失：需补发
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
    """谷仓模拟：查件类型、建查件单、出库单、库存。"""

    def __init__(self, sellable=15):
        self.created, self.sellable = [], sellable

    def ticket_types(self, sm_code=None):
        return [{"value": 1, "name": "妥投未收到"}, {"value": 2, "name": "断更"}, {"value": 13, "name": "留言"}]

    def create_ticket(self, **kw):
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


def test_create_ticket_submits(monkeypatch):
    from app.logistics.goodcang import GoodcangClient

    seen = {}

    def handler(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"code": 0, "message": "Success", "data": {"io_code": "CJ1"}})

    s = get_settings().model_copy(update={"goodcang_app_token": "t", "goodcang_app_key": "k",
                                          "goodcang_min_request_interval_ms": 0})
    c = GoodcangClient(s, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.create_ticket(order_code="G1", reference_no="03-1", tracking_no="N1", it_type_id=2,
                           io_desc="轨迹已5天未更新，请核实", emails=["a@example.com"]) == "CJ1"
    assert seen["path"].endswith("/assistant/create_logistic_ticket")
    assert seen["body"]["save_type"] == 2 and seen["body"]["it_type_id"] == 2  # 客服在工作台点提交：直接提交


def test_after_reply_drafts_locally_then_submit(client, monkeypatch):
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
    draft_code = out["tickets"][0]["io_code"]
    assert draft_code.startswith("DRAFT-") and out["tickets"][0]["type"] == "断更"
    assert gc.created == []  # 草稿只存本系统，不调谷仓写接口
    assert out["stock"]["all_in_stock"]
    with session_scope() as db:
        t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == draft_code))
        assert (t.origin, t.conversation_id, t.store_id, t.it_type_id, t.sm_code) == (
            "AI_DRAFT", conv_id, "STORE_A", 2, "DEDHL")
        assert "8天未更新" in t.io_desc
        assert _tickets_for(db, "03-15386-67153", ["003404"])[0]["submitted"] is False  # 草稿不能说已联系承运商
        note = db.get(Conversation, conv_id).aftersales_note
        assert "可以补发" in note and "已起草查件单（断更）" in note
        out = followup.after_reply(db, job, db.get(Conversation, conv_id), state, reasons, client=gc)
        ticket_id = t.id
    assert out["tickets"][0]["skipped"] == "已有未完结的查件单"

    # 工作台：改查件需求后提交到谷仓 → 换成谷仓单号；再提交、再改、废弃都不行
    from app.api import workbench

    monkeypatch.setattr(workbench, "GoodcangClient", lambda *a, **k: gc)
    r = client.patch(f"/internal/logistics-tickets/{ticket_id}", json={"io_desc": "短"})
    assert r.status_code == 409
    r = client.post(f"/internal/logistics-tickets/{ticket_id}/submit",
                    json={"it_type_id": 13, "type_name": "留言", "io_desc": "请核实包裹状态并催派，谢谢"}).json()
    assert (r["io_code"], r["type"], r["is_local_draft"], r["is_draft"], r["submitted_by"]) == (
        "CJTEST1", "留言", False, False, "tester")
    assert gc.created[0]["it_type_id"] == 13 and gc.created[0]["emails"] == ["a@example.com"]
    assert client.post(f"/internal/logistics-tickets/{ticket_id}/submit", json={}).status_code == 409
    assert client.post(f"/internal/logistics-tickets/{ticket_id}/discard").status_code == 409
    with session_scope() as db:
        assert _tickets_for(db, "03-15386-67153", ["003404"])[0]["submitted"] is True

    monkeypatch.setattr(s, "logistics_ticket_auto_draft", False)  # 关掉自动起草：只查库存
    with session_scope() as db:
        out = followup.after_reply(db, job, db.get(Conversation, conv_id), state, reasons, client=gc)
    assert "tickets" not in out and out["stock"]


def test_ticket_list_views_and_counts(client):
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.models import LogisticsTicket

    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-L1", "buyer_l", None, "EBAY_DE", None)
        db.add_all([
            LogisticsTicket(io_code="CJD1", reference_no="01-00000-00001", tracking_no="T1", store_id="STORE_A",
                            conversation_id=conv.id, type_name="断更", io_status_text="草稿（待在谷仓提交）", origin="AI_DRAFT"),
            LogisticsTicket(io_code="CJR1", reference_no="01-00000-00002", store_id="STORE_A", conversation_id=conv.id,
                            type_name="断更", io_status=7, io_status_text="已回复待完结", reply="已恢复", suggestion="Hi",
                            needs_human=True),
            LogisticsTicket(io_code="CJP1", reference_no="01-00000-00003", store_id="STORE_A", io_status=3,
                            io_status_text="处理中"),
            LogisticsTicket(io_code="CJX1", reference_no="AMZ-1", io_status=8, io_status_text="已完结"),
        ])
    r = client.get("/internal/logistics-tickets", params={"view": "draft"}).json()
    assert [i["io_code"] for i in r["items"]] == ["CJD1"] and r["items"][0]["is_draft"]
    assert r["counts"] == {"draft": 1, "replied": 1, "reply": 1, "human": 1, "open": 2, "done": 1, "all": 4}
    assert {i["io_code"] for i in client.get("/internal/logistics-tickets", params={"view": "open"}).json()["items"]}         == {"CJR1", "CJP1"}
    row = client.get("/internal/logistics-tickets", params={"view": "reply"}).json()["items"][0]
    assert (row["io_code"], row["conversation_id"], row["buyer_key"]) == ("CJR1", conv.id, "buyer_l")
    assert [i["io_code"] for i in client.get("/internal/logistics-tickets",
                                             params={"view": "all", "q": "00003"}).json()["items"]] == ["CJP1"]


def test_discard_local_draft(client):
    from app.db import session_scope
    from app.models import LogisticsTicket

    with session_scope() as db:
        t = LogisticsTicket(io_code="DRAFT-ABC", reference_no="01-00000-00009", tracking_no="T9", store_id="STORE_A",
                            type_name="断更", it_type_id=2, io_desc="请核实包裹状态", origin="AI_DRAFT",
                            io_status_text="草稿（待在系统里提交）")
        db.add(t)
        db.flush()
        tid = t.id
    assert client.get("/internal/logistics-tickets", params={"view": "draft"}).json()["counts"]["draft"] == 1
    r = client.post(f"/internal/logistics-tickets/{tid}/discard").json()
    assert (r["status"], r["is_draft"], r["is_local_draft"]) == ("已废弃（未提交）", False, False)
    counts = client.get("/internal/logistics-tickets").json()["counts"]
    assert (counts["draft"], counts["done"]) == (0, 1)


class PodProvider:
    """谷仓回复“POD 已提供”：不需要人工，起草通知买家的消息。"""

    def __init__(self):
        self.prompts = []

    def structured(self, messages, schema):
        from app.providers.chat import StructuredResult

        self.prompts.append(messages[-1].content)
        human = "丢" in messages[-1].content
        parsed = schema(language="en", reply_text="Hello, the carrier confirmed delivery of GN1. Please find the proof of "
                        "delivery attached.", reply_zh="承运商确认已送达，附上签收证明。", needs_human=human, reason="")
        return StructuredResult(parsed, "", None, {"total_tokens": 10})


def test_ticket_reply_auto_sent_with_pod_even_without_conversation(app_env, monkeypatch, tmp_path):
    from sqlalchemy import select

    from app.aftersales_mail.service import Located
    from app.db import session_scope
    from app.logistics import tickets
    from app.logistics.assess import CN
    from app.models import AftersalesFile, Conversation, LogisticsTicket, Message

    s = get_settings()
    monkeypatch.setattr(s, "aftersales_files_dir", str(tmp_path / "files"))
    monkeypatch.setattr(s, "aftersales_virus_scan", False)
    monkeypatch.setattr(s, "logistics_ticket_auto_send", True)
    recent = (datetime.now(CN) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {"io_code": "CJP1", "reference_no": "09-15357-84924", "tracking_no": "GN1", "it_type_name": "申请POD",
         "io_status": 7, "io_status_text": "已回复待完结", "customer_service_reply": "您好，pod已提供，请查收",
         "add_time": recent, "reply_time": recent,
         "replay_attachment_file_list": [{"name": "GN1.jpg", "url": "https://files.example/gn1.jpg"}]},
        {"io_code": "CJP2", "reference_no": "09-15357-84925", "tracking_no": "GN2", "it_type_name": "断更",
         "io_status": 7, "io_status_text": "已回复待完结", "customer_service_reply": "包裹已丢失", "add_time": recent,
         "reply_time": recent},
    ]
    loc = lambda db, ref: Located(ref, "STORE_A", "buyer_" + ref[-2:], None, "order_api")  # noqa: E731  买家都没来过信
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"\xff\xd8\xff jpeg", headers={"content-type": "image/jpeg"})))
    prov = PodProvider()
    with session_scope() as db:
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert (stats["sent"], stats["needs_human"]) == (1, 1)
    assert "买家还没有来信" in prov.prompts[0]
    with session_scope() as db:
        t1 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJP1"))
        assert (t1.send_status, t1.handled_by) == ("SENT", "AI（查件结果）")
        conv = db.get(Conversation, t1.conversation_id)
        assert (conv.ebay_conversation_id, conv.buyer_key) == ("ORDER-09-15357-84924", "buyer_24")
        msg = db.get(Message, t1.sent_message_id)
        assert msg.sender_role == "SELLER" and msg.body.startswith("Hello") and msg.zh_translation
        f = db.get(AftersalesFile, msg.media[0]["fileId"])
        assert (msg.media[0]["mediaType"], f.status, f.conversation_id) == ("IMAGE", "STORED", conv.id)
        t2 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJP2"))
        assert t2.send_status == "NEEDS_HUMAN" and t2.sent_message_id is None and t2.handled_at is None
        assert db.get(Conversation, t2.conversation_id).aftersales_status == "PENDING"
    with session_scope() as db:  # 再同步：不重复发
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert stats["sent"] == 0 and len(prov.prompts) == 2


def test_auto_submit_and_no_review_for_inquiry(client, monkeypatch):
    """LOGISTICS_TICKET_AUTO_SUBMIT：起草后直接提交到谷仓；这些物流原因不再转人工，告诉买家已发起查件的回复走自动发送。"""
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.agent.nodes import _apply_logistics_auto
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.logistics import followup
    from app.models import JobState, LogisticsTicket

    s = get_settings()
    for k, v in {"logistics_ticket_auto_draft": True, "logistics_ticket_auto_submit": True,
                 "logistics_ticket_notify_emails": "a@example.com", "goodcang_app_token": "t", "goodcang_app_key": "k"}.items():
        monkeypatch.setattr(s, k, v)
    pkg = {"order_id": "03-15386-67154", "carrier": "DHL Paket", "tracking_number": "003405",
           "warehouse": {"order_code": "G1-0002", "shipping_method": "DEDHL", "warehouse_code": "DE"},
           "tracking": {"status": "InTransit", "events": []},
           "assessment": {"stage": "STALLED", "reason_code": "TRACKING_STALLED", "needs_human": True, "days_without_update": 6.0}}
    state = {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg}]}
    gc = FakeGC()
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-AUTO", "buyer_auto", None, "EBAY_DE", None)
        job = SimpleNamespace(conversation_id=conv.id, id=999, decision_reasons=["TRACKING_STALLED"],
                              state=JobState.REVIEW_REQUIRED)
        out = followup.after_reply(db, job, conv, state, ["TRACKING_STALLED"], client=gc)
        assert out["tickets"][0]["submitted"] and gc.created[0]["io_desc"].startswith("eBay订单03-15386-67154")
        t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.reference_no == "03-15386-67154"))
        assert t.io_code == "CJTEST1" and t.submitted_by == "AI（物流规则）"
        assert followup.auto_handled(out) == {"TRACKING_STALLED"}
        # 只有物流原因：改成可自动发送；还有别的原因（如店铺开关没开）照旧要审核
        res = {"reasons": ["TRACKING_STALLED"], "has_draft": True}
        assert _apply_logistics_auto(db, job, 1, res, out) == {"TRACKING_STALLED"}
        assert (job.state, job.decision_reasons) == (JobState.AUTO_ELIGIBLE, [])
        job.state = JobState.REVIEW_REQUIRED
        res = {"reasons": ["TRACKING_STALLED", "STORE_AUTO_SEND_OFF"], "has_draft": True}
        _apply_logistics_auto(db, job, 1, res, out)
        assert (job.state, job.decision_reasons) == (JobState.REVIEW_REQUIRED, ["STORE_AUTO_SEND_OFF"])
        # 再来一次：已有未完结的查件单在跟，也算已处理，不重复建单
        out2 = followup.after_reply(db, job, conv, state, ["TRACKING_STALLED"], client=gc)
        assert out2["tickets"][0]["skipped"] == "已有未完结的查件单" and len(gc.created) == 1
        assert followup.auto_handled(out2) == {"TRACKING_STALLED"}
    monkeypatch.setattr(s, "logistics_ticket_auto_submit", False)  # 关掉：照旧要人工
    assert followup.auto_handled(out) == set()


def test_needs_human_result_also_replied_and_timeline(client, monkeypatch, tmp_path):
    """LOGISTICS_TICKET_SEND_NEEDS_HUMAN：丢失等需要人工的结果也直接回复买家，同时标待售后；对话里按时间显示发起查件和收到反馈。"""
    from sqlalchemy import select

    from app.aftersales_mail.service import Located
    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.logistics import tickets
    from app.logistics.assess import CN
    from app.models import Conversation, LogisticsTicket

    s = get_settings()
    for k, v in {"aftersales_files_dir": str(tmp_path / "files"), "aftersales_virus_scan": False,
                 "logistics_ticket_auto_send": True, "logistics_ticket_send_needs_human": True}.items():
        monkeypatch.setattr(s, k, v)
    recent = (datetime.now(CN) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = [{"io_code": "CJL1", "reference_no": "09-15357-84926", "tracking_no": "GN3", "it_type_name": "断更",
             "io_status": 7, "io_status_text": "已回复待完结", "customer_service_reply": "包裹已丢失", "add_time": recent,
             "reply_time": recent}]
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-LOST", "buyer_lost", None, "EBAY_GB", None)
        _append_message(db, conv, "ML1", "BUYER", "IN", "Where is my parcel?", None)
        conv_id = conv.id
        db.add(LogisticsTicket(io_code="CJL1", reference_no="09-15357-84926", tracking_no="GN3", type_name="断更",
                               store_id="STORE_A", buyer_key="buyer_lost", conversation_id=conv_id, origin="AI_DRAFT",
                               io_desc="轨迹6天未更新，请核实", created_by="AI（物流规则）", created_at=utcnow() - timedelta(hours=3),
                               submitted_by="AI（物流规则）", submitted_at=utcnow() - timedelta(hours=3)))
    loc = lambda db, ref: Located(ref, "STORE_A", "buyer_lost", conv_id, "order_api")  # noqa: E731
    with session_scope() as db:
        stats = tickets.sync(db, client=FakeTickets(rows), provider=PodProvider(), locate=loc,
                             http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert (stats["sent"], stats["needs_human"]) == (1, 1)
    with session_scope() as db:
        t = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJL1"))
        assert t.send_status == "SENT" and t.needs_human
        assert db.get(Conversation, conv_id).aftersales_status == "PENDING"  # 同时标待售后，同事跟进
    timeline = client.get(f"/internal/conversations/{conv_id}").json()["ticket_timeline"]
    assert [x["kind"] for x in timeline] == ["opened", "replied"]
    assert timeline[0]["by"] == "AI（物流规则）" and timeline[0]["desc"].startswith("轨迹6天")
    assert timeline[1]["reply"] == "包裹已丢失" and timeline[1]["send_status"] == "SENT" and timeline[1]["sent_at"]


# ---------------------------------------------------------------- 需补发：损坏、丢件、退回、空包裹、轨迹未更新/长时间没更新
def test_reship_cases_from_tracking():
    from app.logistics.assess import assess

    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    s = get_settings().model_copy(update={"logistics_stall_days": 4, "logistics_reship_days": 7})

    def run(status, sub=None, last="2026-11-09T12:00:00Z", wh=None):
        events = [{"time": last, "time_utc": last}] if last else []
        return assess({"tracking_number": "N1", "tracking": {"status": status, "sub_status": sub, "events": events},
                       "warehouse": wh or {}}, tracking_enabled=True, now=now, settings=s)

    for sub, word in (("Exception_Damage", "损坏"), ("Exception_Lost", "丢失"), ("Exception_Returned", "已退回"),
                      ("Exception_Returning", "退件中")):
        a = run("Exception", sub)
        assert (a["stage"], a["reason_code"], a["needs_human"]) == ("LOST_DAMAGED", "TRACKING_LOST_DAMAGED", True)
        assert word in a["stage_label"] and "补发" in a["stage_label"]
    assert run("Expired")["reason_code"] == "TRACKING_LOST_DAMAGED"  # 运输过久
    assert run("DeliveryFailure", "DeliveryFailure_Rejected")["reason_code"] == "TRACKING_EXCEPTION"  # 拒收：普通异常
    # 有跟踪号一直没轨迹：刚发货只转人工核实；发货满 4 天按断更、满 7 天按长时间未更新，都要判断补发
    fresh = run("NotFound", last=None, wh={"date_shipping": "2026-11-09 08:00:00"})
    assert fresh["reason_code"] == "TRACKING_NOT_FOUND"
    stalled = run("NotFound", last=None, wh={"date_shipping": "2026-11-05 08:00:00"})
    assert (stalled["reason_code"], stalled["stage_label"]) == ("TRACKING_STALLED", "发货后多天查不到轨迹，需判断补发")
    assert run("NotFound", last=None, wh={"date_shipping": "2026-11-01 08:00:00"})["reason_code"] == "TRACKING_RESHIP_REVIEW"


def test_empty_parcel_and_reship_marking(app_env):
    from app.agent.nodes import RESHIP_TYPE, _mark_logistics_aftersales, _tracking_problems
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.logistics.followup import RESHIP_REASONS
    from app.models import Conversation

    for text in ("The box arrived empty!", "Das Paket war leer, nichts drin", "Colis vide", "收到的是空包裹"):
        assert "EMPTY_PARCEL" in _tracking_problems({"observations": []}, text), text
    assert _tracking_problems({"observations": []}, "Empty your cart first?") == []
    assert {"EMPTY_PARCEL", "TRACKING_STALLED", "TRACKING_LOST_DAMAGED"} <= RESHIP_REASONS  # 都会查补发库存
    with session_scope() as db:
        for i, reason in enumerate(("TRACKING_STALLED", "TRACKING_LOST_DAMAGED", "EMPTY_PARCEL")):
            conv = get_or_create_conversation(db, "STORE_A", f"C-R{i}", f"buyer_r{i}", None, "EBAY_DE", None)
            job = type("J", (), {"conversation_id": conv.id, "id": i})()
            assert _mark_logistics_aftersales(db, job, {"observations": []}, [reason]) == RESHIP_TYPE
            assert db.get(Conversation, conv.id).aftersales_type == "物流问题需判断补发"


def test_ticket_reply_reship_words():
    from app.logistics.tickets import HUMAN_WORDS, RESHIP_WORDS

    for reply in ("包裹已丢失", "包裹已退回仓库", "外箱破损", "买家反馈空包", "轨迹仍未更新", "轨迹久未更新"):
        assert RESHIP_WORDS.search(reply) and HUMAN_WORDS.search(reply), reply
    assert not RESHIP_WORDS.search("您好，已尝试帮您催促，请留意后续轨迹更新")
    assert HUMAN_WORDS.search("需要核实地址") and not RESHIP_WORDS.search("需要核实地址")  # 地址问题：转人工但不算补发
