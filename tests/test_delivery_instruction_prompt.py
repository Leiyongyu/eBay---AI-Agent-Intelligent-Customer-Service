"""买家要求放大堂 / 免签收 / 再次派送留备注：回复提示词必须要求买家自己联系承运商，不许说“我们会联系承运商”。

背景：2026-11-10 会话 10832，买家（Business Closed 投递失败）要求再派送时放大堂、免签收，
AI 回复“已转交团队，同事会联系承运商要求放在大堂”。我们改不了派送，口径是请买家拿跟踪号自己联系当地承运商
（知识库 CS-SHP-007 同口径）。
"""
from app.prompts import load_prompt


def _rule() -> str:
    text = load_prompt("reply")
    line = next((ln for ln in text.splitlines() if "派送备注" in ln and "放大堂" in ln), None)
    assert line, "reply.md 缺少“派送备注 / 改派送方式”规则"
    return line


def test_rule_covers_the_buyer_requests():
    line = _rule()
    for word in ("放门口", "放大堂", "放信箱", "不用签收", "再次派送", "Business Closed", "改收货地址"):
        assert word in line, word


def test_rule_tells_buyer_to_contact_carrier_with_tracking():
    line = _rule()
    assert "直接联系当地承运商" in line
    assert "跟踪号" in line and "carrier_website" in line


def test_rule_forbids_promising_we_contact_carrier_even_when_needs_human():
    line = _rule()
    assert "needs_human 为 true" in line
    for banned in ("已转交团队", "同事会联系承运商", "免签收", "已加好备注"):
        assert banned in line, banned
    assert "绝不写" in line


def test_rule_placed_before_generic_no_promise_rule():
    text = load_prompt("reply")
    assert text.index("派送备注或改派送方式") < text.index("不要替买家假设还没发生的情况")
