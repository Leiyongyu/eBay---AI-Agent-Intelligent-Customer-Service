"""异常包裹跟进表自动登记：物流规则、查件单、eBay 纠纷 / 退货 / 退款；负责人（会话处理人 → 店铺客服 → 管理员）；
同一订单只一条、只补空白字段、完结后不重建、可以完结的提示。"""
from datetime import timedelta

from conftest import make_user


def _conv(db, key="PA1", buyer="buyer_pa"):
    from app.ingestion.service import get_or_create_conversation

    return get_or_create_conversation(db, "STORE_A", f"C-{key}", buyer, "110000000001", "EBAY_DE", None)


def _state(order_id="27-40004-US", stage_label="轨迹 6 天没有更新", reason="TRACKING_STALLED"):
    pkg = {"order_id": order_id, "carrier": "DHL", "tracking_number": "00340434756793385199",
           "assessment": {"stage": "STALLED", "stage_label": stage_label, "needs_human": True, "reason_code": reason}}
    order = {"order_id": order_id, "sku": "DAS-10760-0557"}
    return {"observations": [{"tool_name": "get_package_status", "status": "FOUND", "data": pkg},
                             {"tool_name": "get_authorized_order_context", "status": "FOUND", "data": order}]}


def test_owner_rules(client):
    from app import parcel_auto
    from app.db import session_scope, utcnow
    from app.models import WorkClaim

    make_user("cs_store", "CS", stores=["STORE_A"])
    make_user("cs_claim", "CS", stores=["STORE_A"])
    with session_scope() as db:
        conv = _conv(db)
        assert parcel_auto.owner_for(db, "STORE_A", conv.id)[1] == "cs_store"  # 没人处理：店铺的客服
        db.add(WorkClaim(store_id="STORE_A", conversation_id=conv.id, claimed_by="cs_claim", claimed_at=utcnow(),
                         expires_at=utcnow() + timedelta(minutes=10)))
        db.flush()
        assert parcel_auto.owner_for(db, "STORE_A", conv.id)[1] == "cs_claim"  # 会话谁在处理归谁
        assert parcel_auto.owner_for(db, "STORE_X", None)[1] == "tester"  # 店铺没分配人：管理员


def test_from_reply_creates_once_and_fills_blanks(client):
    from app import parcel_auto
    from app.db import session_scope, utcnow
    from app.models import ParcelFollowup
    from sqlalchemy import select

    make_user("cs_pa", "CS", stores=["STORE_A"])
    with session_scope() as db:
        conv = _conv(db)
        ids = parcel_auto.from_reply(db, conv, _state(), ["TRACKING_STALLED", "RISK_NOT_L0"], {"tickets": []})
        r = db.get(ParcelFollowup, ids[0])
        assert (r.origin, r.reason, r.owner_name, r.order_id, r.sku, r.carrier, r.tracking_no) == (
            "AUTO", "断更", "cs_pa", "27-40004-US", "DAS-10760-0557", "DHL", "00340434756793385199")
        assert "轨迹 6 天没有更新" in r.reason_note and r.next_step.startswith("核实物流") and r.conversation_id == conv.id
        r.carrier = "DHL Paket"  # 人工改过的字段：自动补写不覆盖
        follow = {"tickets": [{"io_code": "CJ202611090001", "submitted": True}]}
        assert parcel_auto.from_reply(db, conv, _state(), ["TRACKING_STALLED"], follow) == [r.id]  # 同一订单不重复建
        assert (r.carrier, r.ticket_no, r.next_step) == ("DHL Paket", "CJ202611090001", "已提交谷仓查件单，等查件结果")
        assert parcel_auto.from_reply(db, conv, _state(), ["RISK_NOT_L0"], None) == []  # 不是物流原因：不登记
        r.status, r.closed_at = "DONE", utcnow()
        db.flush()
        assert parcel_auto.from_reply(db, conv, _state(), ["TRACKING_STALLED"], None) == []  # 刚完结：不重建
        assert db.scalars(select(ParcelFollowup)).all() == [r]


def test_tickets_update_record(client):
    from app import parcel_auto
    from app.db import session_scope
    from app.models import LogisticsTicket

    with session_scope() as db:
        conv = _conv(db, "PA2", "buyer_pb")
        t = LogisticsTicket(io_code="CJ202611090002", reference_no="27-50005-US", tracking_no="00340434756793540666",
                            type_name="妥投未收到", conversation_id=conv.id, store_id="STORE_A", buyer_key="buyer_pb")
        db.add(t)
        db.flush()
        r = parcel_auto.on_ticket_submitted(db, t)
        assert (r.reason, r.ticket_no, r.next_step, r.origin) == ("妥投未收到", "CJ202611090002", "已提交谷仓查件单，等查件结果", "AUTO")
        assert r.next_follow_on  # 3 天后跟进
        r2 = parcel_auto.on_ticket_result(db, t, "承运商确认妥投（或已给 POD），买家反馈没收到", "需人工审核")
        assert r2.id == r.id and r.result.startswith("谷仓回复") and "需人工审核" in r.next_step


def test_cases_inr_return_refund(client):
    from app import parcel_auto
    from app.db import session_scope, utcnow
    from app.models import EbayCase, EbayOrderLine, ParcelFollowup
    from sqlalchemy import select

    with session_scope() as db:
        db.add(EbayOrderLine(store_id="STORE_A", order_id="27-60006-US", line_item_id="L1", line_key="110000000001-77",
                             item_id="110000000001", buyer="buyer_pc", sku="MCD-20077-0300"))
        due = utcnow() + timedelta(days=3)
        db.add(EbayCase(store_id="STORE_A", kind="INQUIRY", external_id="5390099631", order_id="110000000001-77",
                        item_id="110000000001", buyer="buyer_pc", status="OPEN", is_open=True, opened_at=utcnow(), due_at=due))
        db.add(EbayCase(store_id="STORE_A", kind="RETURN", external_id="R1", order_id="27-70007-US", buyer="buyer_pd",
                        status="RETURN_REQUESTED", is_open=True, reason="NO_LONGER_NEED_ITEM", opened_at=utcnow()))  # 不想要：不登记
        db.flush()
        parcel_auto.from_cases(db, "STORE_A")
        rows = db.scalars(select(ParcelFollowup)).all()
        assert len(rows) == 1
        r = rows[0]
        assert (r.reason, r.order_id, r.sku, r.next_follow_on) == ("未收到货纠纷", "27-60006-US", "MCD-20077-0300", due.date().isoformat())
        assert "INR" in r.next_step and "5390099631" in r.reason_note
        parcel_auto.from_cases(db, "STORE_A")  # 每小时再跑：不重复
        assert len(db.scalars(select(ParcelFollowup)).all()) == 1
        db.add(EbayCase(store_id="STORE_A", kind="REFUND", external_id="RRP-1", order_id="27-60006-US", buyer="buyer_pc",
                        status="PAYOUT", amount=-22.64, currency="GBP", opened_at=utcnow()))
        db.flush()
        parcel_auto.from_cases(db, "STORE_A")
        assert (r.action, r.amount, r.currency, r.result, r.can_close) == ("退款", 22.64, "GBP", "已退款", True)
        assert "可以完结" in r.next_step and r.status == "OPEN"  # 不自动完结
