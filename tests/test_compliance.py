import pytest

from app.guards.compliance import ComplianceChecker


@pytest.fixture()
def checker(app_env):
    return ComplianceChecker()


@pytest.mark.parametrize("text", [
    "Email me at john.doe@gmail.com",
    "write to john (at) gmail (dot) com",
    "Call us on +1 415 555 0132",
    "Our WhatsApp: +8613800138000",
    "See https://my-parts-shop.com/deal",
    "Just pay me directly via Zelle and I'll ship today.",
    "Leave 5 star feedback and we will refund $10.",
    "Your card 4111 1111 1111 1111 has been noted.",
])
def test_block(checker, text):
    assert checker.check(text).result == "BLOCK", text


@pytest.mark.parametrize("text", [
    "Thanks! It fits 2016-2021 Honda Civic LX, EX and Sport.",
    "Your order 26-10001-US shipped on 2026-10-21.",
    "Please check the listing on https://www.ebay.com/itm/110000000001 for details.",
    "Track it at https://tools.usps.com/go/TrackConfirmAction",
])
def test_pass(checker, text):
    assert checker.check(text).result == "PASS", checker.check(text).to_dict()


@pytest.mark.parametrize("text", [
    # 2026-10-24 真实会话回放中被误拦的大众 OE 号
    "Vielen Dank für die Originalteilenummer (028 103 373 J).",
    "Ob die Originalteilnummer des Zylinderkopfs tatsächlich 028 103 373 J lautet.",
    "The OE number 955 299 26 matches your car.",
    "您的后备箱锁零件号 A211 740 00 36 我们已记录。",
])
def test_part_numbers_are_not_phone_numbers(checker, text):
    assert checker.check(text).result == "PASS", checker.check(text).to_dict()


@pytest.mark.parametrize("text", [
    "Versand mit DHL, Sendungsnummer 00340434756797176618.",
    "Bitte nennen Sie die eBay-Bestellnummer 23-15072-69793 in der Nachricht.",
])
def test_tracking_and_order_numbers_are_not_phone_numbers(checker, text):
    assert checker.check(text).result == "PASS", checker.check(text).to_dict()


def test_real_phone_still_blocked_next_to_letters(checker):
    assert checker.check("Rufen Sie uns an: +49 1511 1121404").result == "BLOCK"
    assert checker.check("Call 415 555 0132 now").result == "BLOCK"


def test_refusal_is_review_not_block(checker):
    r = checker.check("Sorry, we cannot accept PayPal Friends and Family; payment must go through eBay checkout.")
    assert r.result == "REVIEW"


def test_messenger_mention_without_handle_is_review(checker):
    assert checker.check("We can only talk through eBay messages, not WhatsApp.").result == "REVIEW"


def test_evidence_tracking_number_not_flagged_as_phone(checker):
    # 10 位数字可能是电话；出现在本次证据（订单/追踪号）里时放行
    text = "Tracking number 4155550132 shows it is in transit."
    assert checker.check(text).result == "BLOCK"
    assert checker.check(text, allowed_tokens={"4155550132"}).result == "PASS"
    # 超过 15 位一定不是电话（E.164 上限），不需要证据也放行
    assert checker.check("Tracking number 9400100000000000010001 shows it is in transit.").result == "PASS"
