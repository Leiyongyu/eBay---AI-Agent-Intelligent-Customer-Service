"""发票：订单金额与增值税、PDF 留底、审核后邮件发送（模拟 / 真发）、权限（留底只有管理员能看）、
真实会话只能给本人订单开票、买家发来邮箱时登记发票申请并转人工。"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from app import config
from conftest import login, make_user
from test_pipeline import detail, draft_json, send_buyer, use_fake_llm

ISSUER = {"name": "ChengduXiangyanWangluoKejiYouxiangongsi", "address": "wuhouqu wangjianglu\nchengdushi, CN",
          "postcode": "610041", "vat_numbers": {"de": "DE329615047", "GB": "GB345543105"},
          "vat_rates": {"DE": 19, "GB": 20}, "is_active": True, "store_ids": ["STORE_A"]}


def order(**kw):
    o = {"orderId": "24-14589-32244", "creationDate": "2026-10-28T09:00:00.000Z", "orderPaymentStatus": "PAID",
         "buyer": {"username": "buyer_inv", "buyerRegistrationAddress": {
             "fullName": "Jürgen Babianek", "contactAddress": {"addressLine1": "Burgstraße 2", "city": "Kempten",
                                                               "postalCode": "87435", "countryCode": "DE"}}},
         "pricingSummary": {"priceSubtotal": {"value": "134.90", "currency": "EUR"},
                            "deliveryCost": {"value": "21.01", "currency": "EUR"},
                            "priceDiscount": {"value": "-5.00", "currency": "EUR"},
                            "total": {"value": "150.91", "currency": "EUR"}},
         "paymentSummary": {"payments": [{"paymentDate": "2026-10-28T09:01:00.000Z"}], "refunds": []},
         "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
             "fullName": "Jürgen Babianek", "contactAddress": {"addressLine1": "Burgstraße 2", "city": "Kempten",
                                                               "postalCode": "87435", "countryCode": "DE"}}}}],
         "lineItems": [{"legacyItemId": "157849405143", "sku": "LR-HB-01", "title": "Handbremse Steuerelement",
                        "quantity": 2, "lineItemCost": {"value": "134.90", "currency": "EUR"},
                        "purchaseMarketplaceId": "EBAY_DE"}]}
    o.update(kw)
    return o


@pytest.fixture()
def inv_env(client, monkeypatch, tmp_path):
    from app.invoicing import service

    monkeypatch.setenv("INVOICE_DIR", (tmp_path / "invoices").as_posix())
    for k in ("INVOICE_MAIL_ENABLED", "INVOICE_SMTP_USER", "INVOICE_SMTP_PASSWORD", "INVOICE_MAIL_BCC"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVOICE_MAIL_ENABLED", "false")
    config.get_settings.cache_clear()
    orders = {"24-14589-32244": order()}
    monkeypatch.setattr(service, "_order", lambda store_id, order_id: orders.get(order_id) or (_ for _ in ()).throw(
        service.InvoiceError(404, f"本店铺没有订单 {order_id}")))
    r = client.post("/api/invoice-issuers", json=ISSUER)
    assert r.status_code == 200, r.text
    return client


def conv(store="STORE_A", buyer="buyer_inv", channel="simulator"):
    from app.db import session_scope
    from app.ingestion.service import _append_message, get_or_create_conversation

    with session_scope() as db:
        c = get_or_create_conversation(db, store, f"SIM-INV-{store}-{buyer}", buyer, None, "EBAY_DE", "simulator")
        c.channel_source = channel
        _append_message(db, c, f"SIM-IN-INV-{buyer}", "BUYER", "IN", "Ich brauche eine Rechnung", None)
        db.flush()
        return c.id


def test_builder_amounts_vat_and_language():
    from app.invoicing.builder import build

    doc = build(order(), issuer=ISSUER, seller_account="chrom-autoteile", issue_date="2026-10-30")
    assert (doc["language"], doc["currency"], doc["total"], doc["vat_rate"], doc["vat_amount"]) == (
        "de", "EUR", "150.91", 19.0, "24.09")  # 150.91 × 19 ÷ 119
    assert doc["lines"][0] == {"item_id": "157849405143", "sku": "LR-HB-01", "title": "Handbremse Steuerelement",
                               "quantity": 2, "unit_price": "67.45", "amount": "134.90", "vat_collected": False}
    assert (doc["discount"], doc["shipping"], doc["warnings"]) == ("5.00", "21.01", [])
    assert doc["seller"]["vat_number"] == "DE329615047" and doc["ship_to"][-1] == "Deutschland"

    assert doc["tax_mode"] == "included"

    # eBay 代收增值税：订单金额不含税，发票总额 = 订单金额 + eBay 代收税额（买家实付），税额用 eBay 的数
    de = order(lineItems=[{**order()["lineItems"][0], "ebayCollectAndRemitTaxes": [
        {"taxType": "VAT", "amount": {"value": "28.67", "currency": "EUR"}, "collectionMethod": "NET"}]}])
    doc = build(de, issuer=ISSUER, seller_account=None, issue_date="2026-10-30")
    assert (doc["tax_mode"], doc["net_total"], doc["total"], doc["vat_amount"], doc["vat_rate"]) == (
        "ebay_collected", "150.91", "179.58", "28.67", 19.0)
    assert any("由 eBay 代收代缴" in w for w in doc["warnings"])

    us = order(lineItems=[{**order()["lineItems"][0], "ebayCollectAndRemitTaxes": [
        {"taxType": "STATE_SALES_TAX", "amount": {"value": "9.69", "currency": "EUR"}}]}])
    us["fulfillmentStartInstructions"][0]["shippingStep"]["shipTo"]["contactAddress"]["countryCode"] = "US"
    doc = build(us, issuer=ISSUER, seller_account=None, issue_date="2026-10-30", language="en")
    assert doc["vat_rate"] is None and doc["vat_amount"] is None and doc["total"] == "160.60"
    assert doc["ebay_taxes"] == [{"type": "STATE_SALES_TAX", "amount": "9.69", "currency": "EUR"}]
    plain = order()
    plain["fulfillmentStartInstructions"][0]["shippingStep"]["shipTo"]["contactAddress"]["countryCode"] = "HU"
    assert any("没有配置税率" in w for w in build(plain, issuer=ISSUER, seller_account=None,
                                                  issue_date="2026-10-30")["warnings"])
    from app.invoicing.builder import InvoiceDataError
    from app.invoicing.pdf import render

    # 两种发票：普通发票（同 eBay 订单单据）/ 增值税发票（店铺 VAT 模板）；五种语言都能出 PDF
    de["salesRecordReference"] = "423"
    de["lineItems"][0]["ebayCollectAndRemitTaxes"][0]["ebayReference"] = {"name": "OSS", "value": "DE 200 0817 85"}
    for kind in ("NORMAL", "VAT"):
        for lang in ("de", "en", "fr", "it", "es"):
            d = build(de, issuer=ISSUER, seller_account="x", issue_date="2026-10-30", language=lang, kind=kind,
                      store_name="shop")
            assert (d["kind"], d["sales_record"], d["ebay_tax_reference"]) == (kind, "423", ["OSS - DE 200 0817 85"])
            assert render(d, "TEST-1").startswith(b"%PDF")
    with pytest.raises(InvoiceDataError):  # 没有增值税的订单不能开增值税发票
        build(plain, issuer=ISSUER, seller_account=None, issue_date="2026-10-30", kind="VAT")
    bad = order(pricingSummary={**order()["pricingSummary"], "total": {"value": "160.00", "currency": "EUR"}})
    assert any("不一致" in w for w in build(bad, issuer=ISSUER, seller_account=None, issue_date="2026-10-30")["warnings"])


def test_detect_invoice_email():
    from app.invoicing.service import detect_invoice_email

    assert detect_invoice_email(["my mail: tom.k@example.com"], ["I need an invoice"], []) == "tom.k@example.com"
    assert detect_invoice_email(["tom@example.de"], [], ["invoice_vat"]) == "tom@example.de"
    assert detect_invoice_email(["Rechnung bitte an a.b@web.de"], [], []) == "a.b@web.de"
    assert detect_invoice_email(["contact me at tom@example.com"], ["where is my parcel"], ["shipping_status"]) is None
    assert detect_invoice_email(["Ich brauche eine Rechnung"], [], ["invoice_vat"]) is None


def test_issue_review_send_and_archive_permissions(inv_env, tmp_path):
    client = inv_env
    cid = conv()
    make_user("cs_inv", "CS", stores=["STORE_A"])
    cs = TestClient(client.app)
    login(cs, "cs_inv")

    saved = cs.put(f"/internal/conversations/{cid}/invoice-request", json={"email": "juergen@example.de"}).json()
    assert (saved["status"], saved["source"]) == ("OPEN", "manual")
    assert cs.put(f"/internal/conversations/{cid}/invoice-request", json={"email": "bad"}).status_code == 422
    assert [i["id"] for i in client.get("/internal/conversations", params={"group": "invoice"}).json()] == [cid]

    r = cs.post(f"/internal/conversations/{cid}/invoices", json={"order_id": "24-14589-32244"})
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["number"].endswith("-32244") and inv["status"] == "ISSUED" and "snapshot" not in inv
    assert "测试会话：没有核对订单是否属于这个买家" in inv["warnings"]
    pdf = cs.get(f"/internal/invoices/{inv['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")  # 审核人能预览未发出的
    files = list((tmp_path / "invoices" / "STORE_A").rglob("*.pdf"))
    assert len(files) == 1
    from app.db import session_scope
    from app.models import Invoice

    with session_scope() as db:
        assert db.get(Invoice, inv["id"]).sha256 == hashlib.sha256(files[0].read_bytes()).hexdigest()

    # 同一订单再开：号码加后缀，提示已开过；两份都留底
    again = cs.post(f"/internal/conversations/{cid}/invoices", json={"order_id": "24-14589-32244"}).json()
    assert again["number"] == inv["number"] + "-2" and any("已经开过发票" in w for w in again["warnings"])
    assert len(list((tmp_path / "invoices").rglob("*.pdf"))) == 2
    assert cs.post(f"/internal/invoices/{again['id']}/void", json={"reason": "重复"}).json()["status"] == "VOID"

    assert cs.post(f"/internal/invoices/{inv['id']}/send", json={"email": "juergen@example.de"}).status_code == 422
    sent = cs.post(f"/internal/invoices/{inv['id']}/send", json={"email": "juergen@example.de", "confirmed": True})
    assert sent.json()["status"] == "SIMULATED" and sent.json()["sent_by"] == "cs_inv"  # 邮件未开启：只模拟
    assert client.get(f"/internal/conversations/{cid}/invoice").json()["request"]["status"] == "DONE"

    # 发出后的留底：客服看不到，管理员能看、能导出
    assert cs.get(f"/internal/invoices/{inv['id']}/pdf").status_code == 403
    assert cs.get("/api/invoices").status_code == 403
    assert client.get(f"/internal/invoices/{inv['id']}/pdf").status_code == 200
    listed = client.get("/api/invoices").json()
    assert listed["total"] == 2 and listed["items"][1]["buyer"] == "Jürgen Babianek"
    full = client.get(f"/api/invoices/{inv['id']}").json()
    assert full["snapshot"]["bill_to"][0] == "Jürgen Babianek"
    x = client.get("/api/invoices/export")
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_issue_requires_issuer_and_own_order_for_ebay_conversation(inv_env):
    client = inv_env
    b = conv(store="STORE_B")
    # 没有开票主体：增值税发票不行（缺税号）；普通发票读不到 eBay 注册资料（测试店铺不是 eBay 店）也定不了抬头
    r = client.post(f"/internal/conversations/{b}/invoices", json={"order_id": "24-14589-32244", "kind": "VAT"})
    assert r.status_code == 409 and "开票主体" in r.json()["detail"]
    r = client.post(f"/internal/conversations/{b}/invoices", json={"order_id": "24-14589-32244"})
    assert r.status_code == 502 and "抬头" in r.json()["detail"]
    real = conv(buyer="someone_else", channel="ebay")
    r = client.post(f"/internal/conversations/{real}/invoices", json={"order_id": "24-14589-32244"})
    assert r.status_code == 403
    mine = conv(buyer="buyer_inv", channel="ebay")
    assert client.post(f"/internal/conversations/{mine}/invoices",
                       json={"order_id": "24-14589-32244"}).json()["warnings"] == []
    assert client.post(f"/internal/conversations/{mine}/invoices", json={"order_id": "99-0"}).status_code == 404


def test_real_mail_send_with_smtp(inv_env, monkeypatch):
    import smtplib

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            if password != "app-pass":
                raise smtplib.SMTPAuthenticationError(535, b"bad")

        def send_message(self, msg, to_addrs=None):
            sent.append((msg, to_addrs))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("INVOICE_MAIL_ENABLED", "true")
    monkeypatch.setenv("INVOICE_SMTP_USER", "invoice@shop.example")
    monkeypatch.setenv("INVOICE_SMTP_PASSWORD", "wrong")
    monkeypatch.setenv("INVOICE_MAIL_BCC", "archive@shop.example")
    config.get_settings.cache_clear()
    client = inv_env
    cid = conv()
    inv = client.post(f"/internal/conversations/{cid}/invoices", json={"order_id": "24-14589-32244"}).json()
    failed = client.post(f"/internal/invoices/{inv['id']}/send", json={"email": "j@example.de", "confirmed": True}).json()
    assert failed["status"] == "SEND_FAILED" and "客户端专用密码" in failed["send_error"]

    monkeypatch.setenv("INVOICE_SMTP_PASSWORD", "app-pass")
    config.get_settings.cache_clear()
    ok = client.post(f"/internal/invoices/{inv['id']}/send", json={"email": "j@example.de", "confirmed": True}).json()
    assert ok["status"] == "SENT"
    msg, rcpt = sent[0]
    assert rcpt == ["j@example.de", "archive@shop.example"] and "Rechnung" in msg["Subject"]
    att = next(msg.iter_attachments())
    assert att.get_filename() == f"Rechnung_{inv['number']}.pdf" and att.get_content().startswith(b"%PDF")
    assert client.post("/api/invoices/mail-test", json={"to": "me@shop.example"}).json() == {"ok": True}


def test_buyer_email_creates_request_and_goes_to_review(inv_env):
    from app.worker import worker

    client = inv_env
    use_fake_llm(draft_json(intent="invoice_vat", intents=["invoice_vat"], action="answer", claims=[],
                            reply_text="Thank you, we will send the invoice to that address.",
                            reply_zh="谢谢，我们会把发票发到这个邮箱。"))
    sent = send_buyer(client, "Please send the invoice to tom.k@example.com", buyer="buyer_us_001")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert "INVOICE_EMAIL_RECEIVED" in d["job"]["decision_reasons"] and d["job"]["state"] == "REVIEW_REQUIRED"
    info = client.get(f"/internal/conversations/{sent['conversation_id']}/invoice").json()
    req = info["request"]
    assert (req["email"], req["status"], req["source"]) == ("tom.k@example.com", "OPEN", "agent")
    assert req["order_id"]  # 买家只有一笔订单：自动选中
    assert info["issuer"]["name"] == ISSUER["name"]


def test_issue_by_order_number_without_conversation(inv_env, monkeypatch):
    """直接按订单号开票：店铺按订单号自动定（到各店铺 eBay 查），只能给自己负责的店铺开。"""
    from app.aftersales_mail import service as as_service

    client = inv_env
    monkeypatch.setattr(as_service, "lookup_order_api",
                        lambda order_id: ("STORE_A", "buyer_inv") if order_id == "24-14589-32244" else None)
    r = client.post("/internal/invoices", json={"order_id": "24-14589-32244"})
    assert r.status_code == 200, r.text
    inv = r.json()
    assert (inv["store_id"], inv["conversation_id"], inv["status"]) == ("STORE_A", None, "ISSUED")
    assert client.get(inv["pdf_url"]).content.startswith(b"%PDF")
    assert client.post("/internal/invoices", json={"order_id": "11-11111-11111"}).status_code == 404

    make_user("cs_b", "CS", stores=["STORE_B"])
    cs = TestClient(client.app)
    login(cs, "cs_b")
    assert cs.post("/internal/invoices", json={"order_id": "24-14589-32244"}).status_code == 404  # 不是自己的店铺


def test_invoice_page_flow_waiting_review_send_and_notify(inv_env, monkeypatch):
    """买家要发票 → 发票页登记“等买家邮箱” → 买家给邮箱 → 待审核 → 生成 → 审核后发邮件并在会话里提醒买家查收（写发件邮箱）。"""
    from app.worker import worker

    monkeypatch.setenv("INVOICE_SMTP_USER", "invoice@shop.example")
    config.get_settings.cache_clear()
    client = inv_env
    use_fake_llm(draft_json(intent="invoice_vat", intents=["invoice_vat"], action="clarify", claims=[],
                            reply_text="Please send us the email address for the invoice.", reply_zh="请提供接收发票的邮箱。"))
    first = send_buyer(client, "I need an invoice please", buyer="buyer_us_001")
    worker.run_once()
    cid = first["conversation_id"]
    rows = client.get("/internal/invoice-requests").json()["items"]
    assert [(r["conversation_id"], r["status"]) for r in rows] == [(cid, "WAITING_EMAIL")]
    assert client.get("/internal/invoice-requests/count").json() == {"open": 0, "waiting": 1}

    use_fake_llm(draft_json(intent="invoice_vat", intents=["invoice_vat"], action="answer", claims=[],
                            reply_text="Thank you, we will send it shortly.", reply_zh="谢谢，我们会尽快发送。"))
    send_buyer(client, "my email: tom.k@example.com", buyer="buyer_us_001")
    worker.run_once()
    row = client.get("/internal/invoice-requests", params={"status": "OPEN"}).json()["items"][0]
    assert (row["email"], row["buyer_key"]) == ("tom.k@example.com", "buyer_us_001")

    # 人工在发票页改成测试订单并生成、审核发送
    rid = row["id"]
    assert client.put(f"/internal/invoice-requests/{rid}", json={"order_id": "24-14589-32244"}).status_code == 200
    inv = client.post(f"/internal/invoice-requests/{rid}/invoices", json={"order_id": "24-14589-32244"}).json()
    notice = client.get(f"/internal/invoices/{inv['id']}/notice").json()
    assert "invoice@shop.example" in notice["text"] and "tom.k" not in notice["text"] and notice["sender"] == "invoice@shop.example"
    assert client.post(f"/internal/invoice-requests/{rid}/send",
                       json={"invoice_id": inv["id"], "email": "tom.k@example.com"}).status_code == 422
    r = client.post(f"/internal/invoice-requests/{rid}/send",
                    json={"invoice_id": inv["id"], "email": "tom.k@example.com", "confirmed": True}).json()
    assert r["notice"]["error"] is None, r["notice"]
    assert r["invoice"]["status"] == "SIMULATED" and r["notice"]["sent"] and r["request"]["status"] == "DONE"
    last = detail(client, cid)["messages"][-1]
    # 普通发票上不印发票号，提醒消息写订单号
    assert inv["kind"] == "NORMAL" and last["role"] == "SELLER"
    assert "invoice@shop.example" in last["body"] and "24-14589-32244" in last["body"] and inv["number"] not in last["body"]
    assert client.get("/internal/invoice-requests/count").json() == {"open": 0, "waiting": 0}

    # 店铺范围：别的店铺的客服看不到这条申请
    make_user("cs_other", "CS", stores=["STORE_B"])
    cs = TestClient(client.app)
    login(cs, "cs_other")
    assert cs.get("/internal/invoice-requests", params={"status": "all"}).json()["items"] == []
    assert cs.get(f"/internal/invoice-requests/{rid}").status_code == 404


def test_vat_invoice_type_from_buyer_words_and_kind(inv_env):
    """买家提到增值税、公司、税号时，申请默认“增值税发票”；按申请开出 VAT 发票，提醒消息带发票号。"""
    from app.worker import worker

    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("INVOICE_SMTP_USER", "invoice@shop.example")
    config.get_settings.cache_clear()
    try:
        client = inv_env
        use_fake_llm(draft_json(intent="invoice_vat", intents=["invoice_vat"], action="clarify", claims=[],
                                reply_text="Please send us your email.", reply_zh="请提供邮箱。"))
        sent = send_buyer(client, "I need an invoice with VAT for my company", buyer="buyer_us_001")
        worker.run_once()
        row = client.get("/internal/invoice-requests").json()["items"][0]
        assert (row["status"], row["invoice_type"]) == ("WAITING_EMAIL", "VAT")
        rid = row["id"]
        client.put(f"/internal/invoice-requests/{rid}", json={"email": "tom.k@example.com", "order_id": "24-14589-32244"})
        inv = client.post(f"/internal/invoice-requests/{rid}/invoices", json={"order_id": "24-14589-32244"}).json()
        assert (inv["kind"], inv["kind_label"]) == ("VAT", "增值税发票")
        assert inv["number"] in client.get(f"/internal/invoices/{inv['id']}/notice").json()["text"]
        normal = client.post(f"/internal/invoice-requests/{rid}/invoices",
                             json={"order_id": "24-14589-32244", "kind": "NORMAL"}).json()
        assert normal["kind"] == "NORMAL" and not any("已经开过" in w for w in normal["warnings"])  # 不同种类不算重复
        assert sent["conversation_id"] == row["conversation_id"]
    finally:
        monkeypatch_env.undo()
        config.get_settings.cache_clear()


def test_vat_number_prefix_and_oss_warning():
    """eBay 后台税号不带国家前缀，发票上补上；把 eBay 的 OSS 税号当成卖方税号时提醒。"""
    from app.invoicing.builder import build, format_vat

    assert [format_vat(c, v) for c, v in [("DE", "329615047"), ("ES", "N0023367F"), ("FR", "03884106303"),
                                          ("GB", "345543105"), ("IT", "11234740964"), ("DE", "DE 329615047")]] == [
        "DE329615047", "ESN0023367F", "FR03884106303", "GB345543105", "IT11234740964", "DE329615047"]
    issuer = {**ISSUER, "vat_numbers": {"DE": "329615047"}}
    doc = build(order(), issuer=issuer, seller_account=None, issue_date="2026-10-30", kind="VAT")
    assert doc["seller"]["vat_number"] == "DE329615047" and not any("OSS" in w for w in doc["warnings"])
    oss = {**ISSUER, "vat_numbers": {"DE": "DE200081785"}}
    doc = build(order(), issuer=oss, seller_account=None, issue_date="2026-10-30", kind="VAT")
    assert "eBay 的 OSS 税号" in doc["warnings"][0]


def test_seller_header_from_ebay_registration(inv_env, monkeypatch):
    """发票抬头自动取订单所属店铺的 eBay 注册资料；没有开票主体也能开普通发票（默认税率），税号仍取开票主体。"""
    from app.invoicing import service

    regs = {"STORE_A": {"name": "CHENGDU XIANGYAN WANGLUO KEJI YOUXIAN GONGSI", "contact": "chi huang",
                        "address": "3Ceng1hao Wangjianglu1hao20Zhuang\nChengdu, 四川省\nChina", "postcode": "610041"},
            "STORE_B": {"name": None, "contact": "黎力 方", "address": "锦江区东大街\n成都市, 四川省\nChina", "postcode": "610011"}}
    monkeypatch.setattr(service, "registration", lambda store_id: regs.get(store_id))
    client = inv_env
    a = client.post(f"/internal/conversations/{conv()}/invoices", json={"order_id": "24-14589-32244", "kind": "VAT"}).json()
    snap = client.get(f"/api/invoices/{a['id']}").json()["snapshot"]["seller"]
    assert (snap["name"], snap["contact"], snap["postcode"], snap["vat_number"]) == (
        "CHENGDU XIANGYAN WANGLUO KEJI YOUXIAN GONGSI", "chi huang", "610041", "DE329615047")
    b = client.post(f"/internal/conversations/{conv(store='STORE_B')}/invoices", json={"order_id": "24-14589-32244"})
    assert b.status_code == 200, b.text  # 店铺没有开票主体：普通发票照样开，抬头是注册人
    bs = client.get(f"/api/invoices/{b.json()['id']}").json()["snapshot"]["seller"]
    assert (bs["name"], bs["address"].splitlines()[0]) == ("黎力 方", "锦江区东大街")
    assert client.get(f"/internal/invoices/{b.json()['id']}/pdf").content.startswith(b"%PDF")
