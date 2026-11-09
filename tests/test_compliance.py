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


@pytest.mark.parametrize("text", [
    # 2026-11-03 物流测试：德式“日.月.年 时:分”被当成电话拦下
    "Aktueller Status (03.11.2026 03:59, DE): Die Sendung befindet sich im Transport.",
    "- 02.11.2026 21:25, DE: Die Sendung wurde vom Absender eingeliefert.",
    "- 02.11.2026, um 21:25 Uhr: Die Sendung wurde eingeliefert.",
    "- 30/10/2026 15:51 (DE): La spedizione è stata elaborata.",
])
def test_day_first_dates_are_not_phone_numbers(checker, text):
    assert checker.check(text).result == "PASS", checker.check(text).to_dict()


def test_phone_after_day_first_date_still_blocked(checker):
    assert checker.check("Stand 03.11.2026 03:59 – rufen Sie uns an: +49 1511 1121404").result == "BLOCK"
    assert checker.check("Am 03.11.2026 erreichen Sie uns unter 0151 11214040").result == "BLOCK"


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



def test_wetransfer_allowed_for_aftersales_videos():
    """售后请买家用 WeTransfer 发视频：wetransfer.com / we.tl 不算站外链接；其他网盘照样拦。"""
    from app.guards.compliance import ComplianceChecker

    ok = ComplianceChecker().check("Please upload the video at WeTransfer (wetransfer.com, “Get a link”) and send "
                                   "us the link, e.g. https://we.tl/t-abc123 with your order number.")
    assert ok.result == "PASS", ok.to_dict()
    assert ComplianceChecker().check("Please upload it to dropbox.com and send the link").result == "BLOCK"


def test_carrier_sites_of_all_marketplaces_pass():
    """各站点本地承运商官网（dhl.de、dpd.de、gls-pakete.de、myhermes.de、post.at、laposte.fr、poste.it、correos.es 等）
    不算站外链接；系统自己给出的承运商官网（发货通知、carrier_website）一律能通过合规检查。"""
    from app.guards.compliance import ComplianceChecker
    from app.ship_notices import CARRIERS, FALLBACK_URL, carrier_link

    checker = ComplianceChecker()
    for url in ("https://www.dhl.de/", "https://www.dpd.de/", "https://www.gls-pakete.de/", "https://www.myhermes.de/",
                "https://www.hermes.de/", "https://www.post.at/", "https://www.laposte.fr/", "https://www.colissimo.fr/",
                "https://www.poste.it/", "https://www.brt.it/", "https://www.correos.es/", "https://www.seur.com/",
                "https://www.postnl.nl/", "https://www.yodel.co.uk/"):
        r = checker.check(f"Sie können die Sendung hier verfolgen: {url}")
        assert r.result == "PASS", (url, r.to_dict())
    for _, _, url in CARRIERS:
        assert checker.check(f"Website: {url}").result == "PASS", url
    assert checker.check(f"Website: {FALLBACK_URL}").result == "PASS"
    assert checker.check("Tracking: https://dhl.de.evil-track.com/x").result == "BLOCK"  # 冒充域名照样拦
    for carrier, site in (("DHL", "EBAY_DE"), ("Deutsche Post", "EBAY_DE"), ("DPD", "EBAY_GB"), ("GLS", "EBAY_DE"),
                          ("Hermes", "EBAY_DE"), ("Unknown Courier", "EBAY_FR")):
        assert checker.check(f"Website: {carrier_link(carrier, None, site)[1]}").result == "PASS", carrier


def test_track17_site_outside_whitelist_falls_back(monkeypatch):
    """17TRACK 运输商表里的官网不在白名单：改给 17TRACK 首页，不让发送被合规拦截。"""
    from app import ship_notices

    monkeypatch.setattr(ship_notices, "_track17_site", lambda name: "https://www.some-local-courier.example/")
    assert ship_notices.carrier_link("Some Local Courier")[1] == ship_notices.FALLBACK_URL
    monkeypatch.setattr(ship_notices, "_track17_site", lambda name: "https://www.colisprive.com/")
    assert ship_notices.carrier_link("Colis Prive")[1] == "https://www.colisprive.com/"
