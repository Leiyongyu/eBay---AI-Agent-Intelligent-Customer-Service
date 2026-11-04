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
    s = get_settings().model_copy(update={"logistics_stall_days": 4, "logistics_reship_days": 7,
                                          "logistics_skip_sundays": False})  # 周日规则见 test_logistics_rule_fixes

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
    pickup = stage(pkg(status="AvailableForPickup", last="2026-11-07T10:03:42+00:00"))
    assert (pickup["stage"], pickup["needs_human"], pickup["reason_code"]) == ("PICKUP", False, None)
    # 没有轨迹节点时用谷仓揽收时间（东八区）：11-05 20:00 北京 = 11-05 12:00 UTC → 5 天
    a = stage({"tracking_number": "N1", "tracking": {"status": "InTransit", "events": []},
               "warehouse": {"ascan_time": "2026-11-05 20:00:00"}})
    assert (a["stage"], a["days_without_update"]) == ("STALLED", 5.0)


def test_assess_uses_goodcang_delivery_scan():
    """17TRACK 查不到（USPS、Royal Mail 不支持注册）但谷仓有承运商回传的妥投时间：算已签收，不算断更
    （CJ202611042350 / CJ202611042353 误判）。轨迹本身显示异常、丢失的不被妥投时间盖掉。"""
    from app.logistics.assess import assess

    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    s = get_settings().model_copy(update={"logistics_stall_days": 4, "logistics_reship_days": 7,
                                          "logistics_skip_sundays": False})
    wh = {"ascan_time": "2026-10-28 10:00:00", "dscan_time": "2026-10-29 18:30:00"}

    def stage(tracking, on=True, w=wh):
        return assess({"tracking_number": "RM1", "tracking": tracking, "warehouse": w}, tracking_enabled=on, now=now, settings=s)

    for tracking, on in (({}, True), ({"status": "NotFound", "events": []}, True), (None, False),
                         ({"status": "InTransit", "events": [{"time_utc": "2026-10-28T03:00:00Z"}]}, True)):
        a = stage(tracking, on)
        assert (a["stage"], a["needs_human"], a["reason_code"]) == ("DELIVERED", False, None), tracking
        assert a["delivered_at"] == "2026-10-29T10:30+00:00" and a["stage_label"].startswith("已签收")
    assert stage({"status": "Exception", "sub_status": "Exception_Lost", "events": []})["stage"] == "LOST_DAMAGED"
    assert stage({"status": "DeliveryFailure", "events": []})["stage"] == "EXCEPTION"
    # 没有妥投时间：照旧按揽收时间算（10-28 揽收到 11-04 已 7 天 → 需判断补发）
    assert stage({}, w={"ascan_time": "2026-10-28 10:00:00"})["stage"] == "RESHIP_REVIEW"


def test_delivered_by_scan_and_not_received_triggers_pod_ticket():
    from app.agent.nodes import _tracking_problems

    pkg = {"tracking_number": "RM1", "tracking": {}, "warehouse": {"dscan_time": "2026-10-29 18:30:00"},
           "assessment": {"stage": "DELIVERED", "needs_human": False, "delivered_at": "2026-10-29T10:30+00:00"}}
    state = {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg}]}
    assert _tracking_problems(state, "It says delivered but I have not received it.") == ["TRACKING_DELIVERED_NOT_RECEIVED"]
    assert _tracking_problems(state, "Thanks!") == []


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
    # 已有草稿：不重复起草，但草稿没提交不算在跟（L08 误报“已联系承运商”），即使开了自动提交也不算已处理
    assert (out["tickets"][0]["skipped"], out["tickets"][0]["existing"]) == (followup.DRAFT_SKIP, draft_code)
    monkeypatch.setattr(s, "logistics_ticket_auto_submit", True)
    assert followup.auto_handled(out) == set()
    monkeypatch.setattr(s, "logistics_ticket_auto_submit", False)

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
    assert r["notice"] == "SENT" and r["notice_status"] == "SENT"  # 工作台提交后马上安抚买家（德国站 → 德语）
    with session_scope() as db:
        sent = db.scalars(select(Conversation).where(Conversation.id == conv_id)).one()
        from app.models import Message

        body = db.scalars(select(Message.body).where(Message.conversation_id == sent.id, Message.sender_role == "SELLER")).one()
        assert body.startswith("Vielen Dank")
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
        conv.channel_source = "ebay"  # 真实会话才会自动提交到谷仓（测试会话只起草）
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
        # 回复流程末尾：本次自动提交了查件单 → 发安抚消息（德国站 → 德语），只发一次
        from app.agent.nodes import _submit_notice

        assert _submit_notice(db, out) == "SENT" and t.notice_status == "SENT"
        assert _submit_notice(db, out) == "已发过" and _submit_notice(db, out2) is None
    monkeypatch.setattr(s, "logistics_ticket_auto_submit", False)  # 关掉：照旧要人工
    assert followup.auto_handled(out) == set()


class AscanGC(FakeGC):
    def ticket_types(self, sm_code=None):
        return [{"value": 7, "name": "无Ascan"}] + super().ticket_types(sm_code)


def test_no_ascan_type_only_without_ascan_time_and_note_after_marking(client, monkeypatch):
    """没轨迹时：谷仓也没有揽收时间才查“无Ascan”，有揽收时间查“断更”（followup.py 误选无Ascan）。
    回复流程先跑 after_reply 再标售后：标完要把补发库存、查件单补进备注（之前第一次标的备注里没有）。"""
    from types import SimpleNamespace

    from app.agent.nodes import _mark_logistics_aftersales
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation
    from app.logistics import followup
    from app.models import Conversation

    s = get_settings()
    for k, v in {"logistics_ticket_auto_draft": True, "goodcang_app_token": "t", "goodcang_app_key": "k"}.items():
        monkeypatch.setattr(s, k, v)

    def state(order_id, tn, wh_extra):
        pkg = {"order_id": order_id, "carrier": "Royal Mail", "tracking_number": tn,
               "warehouse": {"order_code": f"G-{tn}", "shipping_method": "UKRM", "warehouse_code": "DE", **wh_extra},
               "tracking": {"status": "NotFound", "events": []},
               "assessment": {"stage": "RESHIP_REVIEW", "reason_code": "TRACKING_RESHIP_REVIEW", "needs_human": True,
                              "stage_label": "发货后多天查不到轨迹，需判断补发", "days_without_update": 7.2}}
        return {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg}]}

    reasons = ["TRACKING_RESHIP_REVIEW"]
    with session_scope() as db:
        conv = get_or_create_conversation(db, "STORE_A", "C-ASCAN", "buyer_ascan", None, "EBAY_DE", None)
        job = SimpleNamespace(conversation_id=conv.id, id=1)
        st = state("19-00000-00001", "RM-A", {"ascan_time": "2026-10-28 10:00:00"})
        out = followup.after_reply(db, job, conv, st, reasons, client=AscanGC())
        assert out["tickets"][0]["type"] == "断更"
        assert _mark_logistics_aftersales(db, job, st, reasons)
        followup.append_note(conv, out)
        note = db.get(Conversation, conv.id).aftersales_note
        assert "可以补发" in note and "已起草查件单（断更）" in note
        followup.append_note(conv, out)  # 再调一次不重复追加
        assert db.get(Conversation, conv.id).aftersales_note == note
        out = followup.after_reply(db, job, None, state("19-00000-00002", "RM-B", {}), reasons, client=AscanGC())
        assert out["tickets"][0]["type"] == "无Ascan"


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


def test_classify_goodcang_replies():
    """谷仓回复归类（样本取自真实查件单）：催派/在途、POD、已妥投、自取点 → 自动回复；丢失、退回、破损 → 需判断补发；
    地址问题、妥投未收到、看不懂 → 需人工审核。"""
    from types import SimpleNamespace

    from app.logistics.tickets import AUTO, RESHIP, REVIEW, classify, handling

    cases = [
        ("断更", "您好，已尝试帮您催促，请留意后续轨迹更新", None, "IN_TRANSIT"),
        ("无Ascan", "您好，该订单已尝试催派，轨迹久未更新可能存在服务商漏扫描的情况，您可留意后续轨迹更新，谢谢", None, "IN_TRANSIT"),
        ("断更", "您好 该订单尝试反馈服务商催派，后续留意轨迹更新，如您的包裹需要理赔 且符合服务商理赔规范 先按丢件要求申请索赔开查",
         None, "IN_TRANSIT"),  # 附带的理赔建议不算丢件
        ("断更", "您好，10.8日轨迹有更新，包裹正在运输途中，请关注后续轨迹。", None, "IN_TRANSIT"),
        ("申请POD", "获取成功", [{"url": "u"}], "POD"),
        ("申请POD", "", [{"url": "u"}], "POD"),
        ("断更", "您好，轨迹显示已妥投，麻烦与收件人核实是否收到包裹", None, "DELIVERED"),
        ("查自取地址", "您好，轨迹显示已妥投，无自取地址，麻烦与收件人核实是否收到包裹", None, "DELIVERED"),
        ("查自取地址", "您好，无自取地址，已尝试帮您催促，请留意后续轨迹更新", None, "IN_TRANSIT"),
        ("查自取地址", "您好，未退回，轨迹显示包裹等待自取，地址：Ely Delivery Office, Cresswells Lane", None, "PICKUP"),
        ("断更", "您好，轨迹久未更新，未收到请考虑包裹丢失，这票可以提交理赔申请", None, "LOST"),
        ("断更", "您好，经核实反馈由于该包裹久未更新，收件人如未收货可考虑丢件。", None, "LOST"),
        ("断更", "包裹已丢失", None, "LOST"),
        ("退回原因", "您好 包裹10.15退回无法再派", None, "RETURNED"),
        ("退回原因", "您好，官网显示在货件地址处找不到收件人，派件失败，包裹已退回寄件人", None, "RETURNED"),
        ("断更", "外箱破损", None, "DAMAGED"),
        ("退回原因", "您好，轨迹显示未退回，地址错误/信息不完整，无法继续派送，请指示处理意见，谢谢", None, "ADDRESS"),
        ("断更", "您好，派送无人接收", None, "ADDRESS"),
        ("妥投未收到", "您好，pod已提供，请查收", [{"url": "u"}], "DELIVERED_DISPUTE"),
        ("妥投未收到", "您好，投递地址：97 first avenue，GPS定位显示在收件地址附近", None, "DELIVERED_DISPUTE"),
        ("改派", "您好，已尝试帮您催促", None, "ACTION"),
        ("退回原因", "您好，服务商仓库操作超期，但未告知具体原因", None, "UNKNOWN"),
    ]
    for type_name, reply, files, want in cases:
        assert classify(SimpleNamespace(type_name=type_name, reply=reply, attachments=files)) == want, reply
    assert (handling("POD"), handling("LOST"), handling("ADDRESS"), handling(None)) == (AUTO, RESHIP, REVIEW, REVIEW)


def test_ticket_result_auto_reply_rereply_tags_and_unknown(client, monkeypatch, tmp_path):
    """已催派 → 自动回复，已在售后的会话把结果补进备注；谷仓再次回复（说丢件）→ 按新回复重新处理：
    需判断补发、打“需判断补发”标记、售后类型升级；看不懂的回复不发给买家，打“需人工审核”。"""
    from sqlalchemy import select

    from app.aftersales_mail.service import Located
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.logistics import tickets
    from app.logistics.assess import CN
    from app.models import Conversation, ConversationMark, LogisticsTicket, MessageTag

    s = get_settings()
    for k, v in {"aftersales_files_dir": str(tmp_path / "files"), "aftersales_virus_scan": False,
                 "logistics_ticket_auto_send": True, "logistics_ticket_send_needs_human": True}.items():
        monkeypatch.setattr(s, k, v)
    recent = (datetime.now(CN) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    def row(code, ref, reply, type_name="断更"):
        return {"io_code": code, "reference_no": ref, "tracking_no": "T" + code, "it_type_name": type_name, "io_status": 7,
                "io_status_text": "已回复待完结", "customer_service_reply": reply, "add_time": recent, "reply_time": recent}

    with session_scope() as db:
        convs = {}
        for key in ("A", "B"):
            c = get_or_create_conversation(db, "STORE_A", f"C-RE{key}", f"buyer_re{key}", None, "EBAY_GB", None)
            _append_message(db, c, f"MRE{key}", "BUYER", "IN", "Where is my parcel?", None)
            convs[key] = c.id
        a = db.get(Conversation, convs["A"])  # 物流规则已标过待售后（断更）
        a.aftersales_status, a.aftersales_type, a.aftersales_note = "PENDING", "物流异常/未收到", "轨迹多天未更新"
        a.aftersales_marked_by = "AI（物流规则）"
    refs = {"09-15357-80001": convs["A"], "09-15357-80002": convs["B"]}
    loc = lambda db, ref: Located(ref, "STORE_A", "buyer", refs[ref], "order_api")  # noqa: E731
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    rows = [row("CJR1", "09-15357-80001", "您好，已尝试帮您催促，请留意后续轨迹更新"),
            row("CJR2", "09-15357-80002", "您好，服务商仓库操作超期，但未告知具体原因", "退回原因")]
    prov = PodProvider()
    with session_scope() as db:
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert (stats["sent"], stats["needs_human"]) == (1, 1)
    assert "已催派 / 运输中" in prov.prompts[0] and "不需要人工" in prov.prompts[0]

    def tags(db, conv_id):
        return {db.get(MessageTag, m.tag_id).name for m in
                db.scalars(select(ConversationMark).where(ConversationMark.conversation_id == conv_id))}

    with session_scope() as db:
        t1 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJR1"))
        assert (t1.outcome, t1.needs_human, t1.send_status) == ("IN_TRANSIT", False, "SENT")
        a = db.get(Conversation, convs["A"])
        assert "【已催派 / 运输中，请留意轨迹】" in a.aftersales_note and "已自动回复买家" in a.aftersales_note
        assert "需判断补发" not in tags(db, a.id)
        t2 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJR2"))
        assert (t2.outcome, t2.send_status, t2.sent_message_id) == ("UNKNOWN", "NEEDS_HUMAN", None)  # 看不懂：不发
        b = db.get(Conversation, convs["B"])
        assert (b.aftersales_status, b.aftersales_type) == ("PENDING", "物流异常/未收到")
        assert "需人工审核" in tags(db, b.id)
        first_msg = t1.sent_message_id

    rows[0] = row("CJR1", "09-15357-80001", "您好，轨迹久未更新，未收到请考虑包裹丢失")  # 谷仓第二次回复
    with session_scope() as db:
        stats = tickets.sync(db, client=FakeTickets(rows), provider=prov, locate=loc, http=http)
    assert (stats["suggested"], stats["needs_human"], stats["sent"]) == (1, 1, 1)  # 只重做变了的那张
    with session_scope() as db:
        t1 = db.scalar(select(LogisticsTicket).where(LogisticsTicket.io_code == "CJR1"))
        assert (t1.outcome, t1.needs_human, t1.send_status) == ("LOST", True, "SENT") and t1.sent_message_id != first_msg
        a = db.get(Conversation, convs["A"])
        assert a.aftersales_type == "物流问题需判断补发" and a.aftersales_note.startswith("谷仓查件单 CJR1（断更）已回复【包裹疑似丢失")
        assert "补发库存" in a.aftersales_note and "需判断补发" in tags(db, a.id)
    row1 = next(x for x in client.get(f"/internal/conversations/{convs['A']}/logistics-tickets").json()
                if x["io_code"] == "CJR1")
    assert (row1["outcome"], row1["handling"]) == ("LOST", "需判断补发")


def test_submit_notice_reassures_buyer_and_closes_pending_job(client, monkeypatch):
    """查件单提交到谷仓后马上给买家发安抚消息（买家语言，固定话术），会话里待审的 AI 草稿一并结案；
    买家还问了别的、或有其他需人工的原因时不发，留给人工。谷仓回复后再由查件单同步发结果。"""
    from sqlalchemy import select

    from app.db import session_scope, utcnow
    from app.ingestion.service import _append_message, get_or_create_conversation
    from app.logistics import followup
    from app.models import Draft, JobState, LogisticsTicket, Message, ReplyJob

    def setup(key, site, intents, reasons, language="de"):
        conv = get_or_create_conversation(db, "STORE_A", f"C-N{key}", f"buyer_n{key}", None, site, None)
        m = _append_message(db, conv, f"MN{key}", "BUYER", "IN", "Wo ist mein Paket?", None)
        job = ReplyJob(store_id="STORE_A", conversation_id=conv.id, anchor_source_message_id=m.ebay_message_id,
                       state=JobState.REVIEW_REQUIRED, decision_reasons=reasons)
        db.add(job)
        db.flush()
        db.add(Draft(job_id=job.id, run_generation=1, conversation_row_version=conv.row_version, text="Entwurf",
                     language=language, intents=intents, review_reasons=reasons))
        t = LogisticsTicket(io_code=f"CJN{key}", reference_no=f"03-00000-0000{key}", tracking_no=f"N{key}",
                            conversation_id=conv.id, store_id="STORE_A", submitted_at=utcnow())
        db.add(t)
        db.flush()
        return conv, job, t

    with session_scope() as db:
        conv, job, t = setup("1", "EBAY_DE", ["shipping_status"], ["MODEL_REQUESTED_HUMAN", "TRACKING_STALLED"])
        assert followup.send_submit_notice(db, [t]) == "SENT"
        msg = db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.sender_role == "SELLER")).one()
        assert msg.body.startswith("Vielen Dank") and "Logistik-Team" in msg.body and msg.zh_translation.startswith("感谢")
        assert job.state == JobState.SENT and t.notice_status == "SENT" and t.notice_sent_at
        assert followup.send_submit_notice(db, [t]) == "已发过"  # 不重复发

        _, job2, t2 = setup("2", "EBAY_GB", ["shipping_status", "fitment"], ["MODEL_REQUESTED_HUMAN"], language="en")
        assert followup.send_submit_notice(db, [t2]).startswith("买家还问了其他问题")
        assert job2.state == JobState.REVIEW_REQUIRED and t2.notice_sent_at is None

        _, job3, t3 = setup("3", "EBAY_GB", ["shipping_status"], ["INVOICE_EMAIL_RECEIVED"], language="en")
        assert followup.send_submit_notice(db, [t3]).startswith("还有其他需人工的原因")

        conv4, _, t4 = setup("4", "EBAY_FR", ["delivery_delay"], ["TRACKING_RESHIP_REVIEW"], language=None)
        assert followup.send_submit_notice(db, [t4]) == "SENT"  # 草稿没写语言：按站点用法语
        body = db.scalars(select(Message.body).where(Message.conversation_id == conv4.id, Message.sender_role == "SELLER")).one()
        assert body.startswith("Merci")
        monkeypatch.setattr(get_settings(), "logistics_ticket_submit_notice", False)
        _, _, t5 = setup("5", "EBAY_IT", ["shipping_status"], [], language="it")
        assert followup.send_submit_notice(db, [t5]).startswith("未开启")
    for lang, text in followup.NOTICE_TEXT.items():  # 固定话术本身要能通过合规检查
        from app.guards.compliance import ComplianceChecker

        assert ComplianceChecker().check(text).result != "BLOCK", lang
