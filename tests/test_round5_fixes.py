"""第五轮验收遗留（2026-11-10）：K4 “请买家补资料”判断收紧、买家下一轮没补资料仍算追问；K5 致谢词补全；
黑名单“全部店铺”优先（客户管理、eBay 售后页的 blacklist_map 与工作台、详情页一致）。"""
from datetime import timedelta

import pytest

from test_launch_quality import bj, conv


@pytest.mark.parametrize("text", [
    "您好，发票已从我的邮箱发给您，请查收。",
    "Hello, the invoice has been sent to your e-mail, please check.",
    "根据您提供的 VIN，厂家查了不适配，建议退货。",
    "Sehr geehrter Kunde, aufgrund der von Ihnen angegebenen Fahrgestellnummer muss ich Ihnen leider mitteilen, "
    "dass das Teil nicht passt.",
    "Based on the VIN you provided, the part does not fit.",
])
def test_not_info_request(text):
    from app.round_metrics import is_info_request

    assert not is_info_request(text)


@pytest.mark.parametrize("text", [
    "Please send VIN", "Könnten Sie uns bitte die Fahrgestellnummer schicken?", "Could you send a photo?",
    "Thank you for your message. To check the fitment, please send us your VIN. Best regards",
    "请提供车架号，我们帮您核对。", "Teilen Sie uns bitte Ihre Fahrgestellnummer mit.",
])
def test_info_request(text):
    from app.round_metrics import is_info_request

    assert is_info_request(text)


@pytest.mark.parametrize("text, supplies", [
    ("WVWZZZ1KZBW000123", True), ("Baujahr 2015, Golf 7", True), ("my email is a@b.de", True), ("", True),
    ("Sorry, I threw the old sensor away, I can't take a photo", True), ("Siehe Foto im Anhang", True),
    ("按你们的描述应该是适配的，我已经装上了", False), ("What are you selling?", False),
])
def test_supplies_info(text, supplies):
    from app.round_metrics import supplies_info

    assert supplies_info(text) is supplies


def test_follow_up_after_info_request_without_info_counts():
    """我们请买家发 VIN：买家发来 VIN 不算追问；买家回“What are you selling?”仍算追问。"""
    from app.round_metrics import build_rounds, compute

    t0 = bj(2026, 10, 13, 10)

    def rate(reply):
        rounds = build_rounds([conv("V", [(t0, "BUYER", "Does it fit my car?"),
                                          (t0 + timedelta(hours=1), "SELLER", "Please send VIN"),
                                          (t0 + timedelta(hours=5), "BUYER", reply), (t0 + timedelta(hours=6), "SELLER")])])
        return compute(rounds, {}, bj(2026, 12, 1, 10), [], 7)["follow_up"]["followed"]

    assert rate("WVWZZZ1KZBW000123") == 0
    assert rate("What are you selling?") == 1


@pytest.mark.parametrize("text", ["Besten Dank Bewertung ist auch raus", "Dank!", "Vielen herzlichen Dank", "Merci beaucoup",
                                  "Grazie mille", "Muchas gracias"])
def test_more_thanks_words(text):
    from app.round_metrics import is_thanks_only

    assert is_thanks_only(text)


def test_blacklist_map_all_stores_first(app_env):
    from app.customers import blacklist_entry, blacklist_map
    from app.db import session_scope
    from app.models import BuyerBlacklist

    with session_scope() as db:
        db.add(BuyerBlacklist(buyer="both", store_id="STORE_A", reason="店铺专属", created_by="t"))
        db.add(BuyerBlacklist(buyer="both", store_id=None, reason="全部店铺", created_by="t"))
        db.add(BuyerBlacklist(buyer="only_a", store_id="STORE_A", reason="店铺专属", created_by="t"))
        db.add(BuyerBlacklist(buyer="other", store_id="STORE_B", reason="别的店铺", created_by="t"))
    with session_scope() as db:
        m = blacklist_map(db, {("STORE_A", "both"), ("STORE_A", "only_a"), ("STORE_A", "other")})
        assert m[("STORE_A", "both")].reason == "全部店铺" == blacklist_entry(db, "STORE_A", "both").reason
        assert m[("STORE_A", "only_a")].reason == "店铺专属"
        assert ("STORE_A", "other") not in m
