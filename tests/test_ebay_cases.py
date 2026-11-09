"""售后管理（只读）：eBay 退货、咨询、纠纷、取消、付款纠纷、退款单、评价的解析、去重更新、列表与计数、店铺范围、跳到买家会话。"""
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from conftest import login, make_user

RET = {"returnId": "5300001", "orderId": "01-111-222", "buyerLoginName": "buyer_ret", "state": "RETURN_STARTED",
       "status": "WAITING_FOR_RETURN_SHIPMENT", "currentType": "MONEY_BACK",
       "creationInfo": {"item": {"itemId": "110000000001"}, "reason": "NO_LONGER_NEED_ITEM", "comments": {"content": "not needed"},
                        "creationDate": {"value": "2026-11-01T10:00:00.000Z"}},
       "sellerTotalRefund": {"estimatedRefundAmount": {"value": 49.9, "currency": "EUR"}},
       "sellerResponseDue": {"activityDue": "SELLER_APPROVE_REQUEST", "respondByDate": {"value": "2026-11-06T10:00:00.000Z"}},
       "sellerAvailableOptions": [{"actionType": "APPROVE", "actionURL": "https://www.ebay.de/rt/ReturnDetails?returnId=5300001"}]}
INQ = {"itemId": 110000000001, "transactionId": 999, "inquiryId": 77, "buyer": "buyer_inr", "inquiryStatusEnum": "OPEN",
       "claimAmount": {"value": 20.0, "currency": "USD"}, "respondByDate": {"value": "2026-11-07T00:00:00.000Z"},
       "creationDate": {"value": "2026-11-02T00:00:00.000Z"}, "lastModifiedDate": {"value": "2026-11-03T00:00:00.000Z"}}
FEEDBACK_XML = """<GetFeedbackResponse xmlns="urn:ebay:apis:eBLBaseComponents"><FeedbackDetailArray>
<FeedbackDetail><CommentingUser>buyer_fb</CommentingUser><CommentText>Wrong part</CommentText><CommentTime>2026-11-03T08:00:00.000Z</CommentTime>
<CommentType>Negative</CommentType><ItemID>110000000001</ItemID><ItemTitle>Turbo</ItemTitle><FeedbackID>F1</FeedbackID></FeedbackDetail>
</FeedbackDetailArray></GetFeedbackResponse>"""


def test_parsers():
    from app import ebay_cases as ec
    from app.sources.ebay import NS

    r = ec.parse_return(RET)
    assert (r["external_id"], r["order_id"], r["buyer"], r["is_open"], r["amount"], r["reason"]) == (
        "5300001", "01-111-222", "buyer_ret", True, 49.9, "NO_LONGER_NEED_ITEM")
    assert r["due_at"].isoformat() == "2026-11-06T10:00:00" and r["detail"]["action_url"].endswith("returnId=5300001")
    i = ec.parse_inquiry(INQ)
    assert (i["external_id"], i["order_id"], i["is_open"], i["currency"]) == ("77", "110000000001-999", True, "USD")
    assert ec.parse_case({**INQ, "caseId": 5, "caseStatusEnum": "CS_CLOSED"})["is_open"] is False
    c = ec.parse_cancel({"cancelId": "9", "legacyOrderId": "110-9", "cancelState": "CLOSED", "cancelStatus": "CANCEL_CLOSED_WITH_REFUND",
                         "lineItems": [{"itemId": "110", "itemTitle": "Pump"}]})
    assert (c["is_open"], c["title"]) == (False, "Pump")
    d = ec.parse_dispute({"paymentDisputeId": "PD1", "paymentDisputeStatus": "ACTION_NEEDED", "amount": {"value": "10", "currency": "GBP"}})
    assert (d["is_open"], d["amount"]) == (True, 10.0)
    f = ec.parse_feedback(ET.fromstring(FEEDBACK_XML).find("e:FeedbackDetailArray/e:FeedbackDetail", NS), NS)
    assert (f["external_id"], f["rating"], f["is_open"], f["comment"]) == ("F1", "Negative", True, "Wrong part")


def test_save_list_counts_scope_and_conversation(client):
    from app import ebay_cases as ec
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation

    with session_scope() as db:
        assert ec.save(db, "STORE_A", "RETURN", [ec.parse_return(RET)]) == {"new": 1, "changed": 0, "total": 1}
        ec.save(db, "STORE_B", "INQUIRY", [ec.parse_inquiry(INQ)])
        conv = get_or_create_conversation(db, "STORE_A", "EB-RET", "buyer_ret", None, "EBAY_DE", "listing")
        cid = conv.id
    rows = client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()
    assert len(rows) == 1 and rows[0]["is_new"] and rows[0]["conversation_id"] == cid and rows[0]["store_name"]
    # 页面显示的中文名：状态、原因、详情字段
    assert rows[0]["status_label"] and rows[0]["status_label"] != rows[0]["status"]
    assert rows[0]["reason_label"] and rows[0]["reason_label"] != rows[0]["reason"]
    assert all(x["key_label"] for x in rows[0]["detail_labels"])
    counts = {k["kind"]: k for k in client.get("/internal/ebay-cases/counts").json()["kinds"]}
    assert counts["RETURN"]["open"] == 1 and counts["INQUIRY"]["open"] == 1 and counts["FEEDBACK"]["total"] == 0

    # 看过后不再是“新”；状态变了重新算“新”
    client.post(f"/internal/ebay-cases/{rows[0]['id']}/seen")
    assert not client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()[0]["is_new"]
    with session_scope() as db:
        closed = {**RET, "state": "CLOSED", "status": "CLOSED"}
        assert ec.save(db, "STORE_A", "RETURN", [ec.parse_return(closed)])["changed"] == 1
    row = client.get("/internal/ebay-cases", params={"kind": "RETURN"}).json()[0]
    assert row["is_new"] and not row["is_open"]
    assert client.get("/internal/ebay-cases", params={"kind": "RETURN", "open_only": "true"}).json() == []

    # 店铺范围：只负责 STORE_A 的客服看不到 STORE_B 的咨询；同步只有管理员能点
    make_user("cs_cases", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_cases")
    assert cs.get("/internal/ebay-cases", params={"kind": "INQUIRY"}).json() == []
    assert cs.post("/internal/ebay-cases/sync").status_code == 403
