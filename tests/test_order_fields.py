"""订单工具给客服的字段：发货期限、预计送达、付款/取消/退款状态等；买家姓名、地址、电话、邮箱一律不取。"""
import json

from app.agent.schemas import RuntimeContext
from app.sources.ebay import _order_row
from app.tools.readonly import OrderRefArgs, get_authorized_order_context

ORDER = {
    "orderId": "07-15359-46775", "legacyOrderId": "07-15359-46775", "creationDate": "2026-10-28T02:08:02.000Z",
    "orderFulfillmentStatus": "NOT_STARTED", "orderPaymentStatus": "PAID",
    "buyer": {"username": "real_buyer", "buyerRegistrationAddress": {"fullName": "Jane Doe", "email": "j@x.com",
                                                                     "primaryPhone": {"phoneNumber": "0123"}}},
    "pricingSummary": {"total": {"value": "269.17", "currency": "GBP"}},
    "cancelStatus": {"cancelState": "NONE_REQUESTED", "cancelRequests": []},
    "paymentSummary": {"payments": [{"paymentDate": "2026-10-28T02:08:02.824Z"}], "refunds": []},
    "fulfillmentStartInstructions": [{"minEstimatedDeliveryDate": "2026-10-30T00:00:00.000Z",
                                      "maxEstimatedDeliveryDate": "2026-10-31T00:00:00.000Z",
                                      "shippingStep": {"shippingServiceCode": "UK_OtherCourier48",
                                                       "shipTo": {"fullName": "Jane Doe", "email": "j@x.com",
                                                                  "contactAddress": {"addressLine1": "1 High St",
                                                                                     "countryCode": "GB"}}}}],
    "fulfillmentHrefs": [],
    "lineItems": [{"legacyItemId": "398357475290", "sku": "DAS-10208-0098", "title": "Cylinder Head", "quantity": 1,
                   "listingMarketplaceId": "EBAY_GB", "lineItemFulfillmentStatus": "NOT_STARTED",
                   "lineItemFulfillmentInstructions": {"shipByDate": "2026-10-29T23:59:59.000Z",
                                                       "guaranteedDelivery": False}}],
}


class Src:
    name = "fake"

    def find_orders(self, store_id, buyer_key):
        return [_order_row(ORDER)] if buyer_key == "real_buyer" else []


def test_order_row_has_shipping_dates_and_no_personal_data():
    row = _order_row(ORDER)
    assert row["ship_by_date"] == "2026-10-29T23:59:59.000Z"
    assert row["estimated_delivery"] == {"min": "2026-10-30T00:00:00.000Z", "max": "2026-10-31T00:00:00.000Z"}
    assert (row["payment_status"], row["order_status"], row["cancel_state"], row["refunds"]) == (
        "PAID", "NOT_STARTED", "NONE_REQUESTED", 0)
    assert row["paid_at"].startswith("2026-10-28T02:08:02") and row["total"] == {"value": "269.17", "currency": "GBP"}
    assert (row["shipping_service"], row["ship_to_country"], row["has_shipment"]) == ("UK_OtherCourier48", "GB", False)
    text = json.dumps(row)
    assert not any(x in text for x in ("Jane", "j@x.com", "0123", "High St", "real_buyer"))  # 不含个人信息


def test_tool_returns_new_fields_and_drops_empty():
    ctx = RuntimeContext(store_id="S", conversation_id=1, buyer_key="real_buyer", marketplace_id="EBAY_GB",
                         item_id=None, job_id=1, run_generation=1)
    status, data, ref, _ = get_authorized_order_context(ctx, Src(), OrderRefArgs())
    assert (status, ref) == ("FOUND", "order:07-15359-46775")
    assert data["ship_by_date"] and data["estimated_delivery"] and data["title"] == "Cylinder Head"
    assert "items" not in data and "guaranteed_delivery" not in data  # 空值不给模型
