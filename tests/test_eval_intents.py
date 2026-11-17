"""多轮意图评测（scripts/eval_intents.py）的用例格式和评分逻辑。不调用模型。"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("eval_intents", ROOT / "scripts" / "eval_intents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_case_file_is_valid_and_covers_hard_parts(ev):
    from app.intents import INTENTS

    data = json.loads(ev.CASES_FILE.read_text(encoding="utf-8"))
    assert ev.validate(data, set(INTENTS)) == []
    cases = data["cases"]
    assert 30 <= len(cases) <= 50
    tags = {t for c in cases for t in c["tags"]}
    assert {"short_followup", "after_our_reply", "topic_switch", "multi_issue", "thanks_closing"} <= tags
    assert {c["language"] for c in cases} >= {"de", "en", "fr", "it", "es"}
    # 脱敏：对话里不出现邮箱、电话样式
    import re

    text = "\n".join(m["text"] for c in cases for m in c["messages"])
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text)
    assert not re.search(r"(?<![A-Za-z0-9])\+?\d[\d \-]{9,}\d", text)  # 字母开头的是虚构跟踪号，不算


def test_validate_reports_bad_cases(ev):
    bad = {"listings": {"x": {}}, "cases": [
        {"case_id": "A", "listing": "nope", "tags": ["weird"], "messages": [{"role": "BUYER", "text": "hi", "ago": "1x"}],
         "expect": {"intent": ["not_an_intent"], "action": "maybe"}},
        {"case_id": "A", "listing": "x", "tags": [], "messages": [{"role": "BUYER", "text": "a", "ago": "1h"},
                                                                  {"role": "SELLER", "text": "b", "ago": "2h"}],
         "expect": {"intent": ["other"]}},
    ]}
    errs = " | ".join(ev.validate(bad, {"other"}))
    for part in ("listing 'nope'", "未知难点类型 weird", "时间格式不对", "not_an_intent", "expect.action", "编号重复",
                 "从早到晚", "没有买家来信"):
        assert part in errs, part


def test_parse_ago_and_split(ev):
    assert ev.parse_ago("2d20h") == timedelta(days=2, hours=20)
    assert ev.parse_ago("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        ev.parse_ago("")
    msgs = [{"role": "BUYER", "ago": "2d"}, {"role": "SELLER", "ago": "1d"}, {"role": "BUYER", "ago": "1h"},
            {"role": "BUYER", "ago": "5m"}]
    history, inbound = ev.split_messages(msgs)
    assert len(history) == 2 and len(inbound) == 2
    assert ev.split_messages([{"role": "BUYER", "ago": "1h"}]) == ([], [{"role": "BUYER", "ago": "1h"}])


def test_score_rules(ev):
    case = {"expect": {"intent": ["shipping_status", "delivery_delay"], "intents_required": ["shipping_status", "address_change"],
                       "intent_not": ["thanks_closing"], "inherits_issue": True, "action": "handoff"},
            "prior_issue": {"intent": "shipping_status", "summary_zh": "问包裹在哪"}}
    s = ev.score(case, {"intent": "delivery_delay", "intents": ["shipping_status"], "action": "handoff",
                        "inherits_supported": True, "inherits_issue": "m1"})
    assert s == {"primary_ok": True, "required_total": 2, "required_hit": 1, "action_ok": True, "inherits_ok": True}
    # 主意图不在可接受列表、action 不符、沿用判断错
    s = ev.score(case, {"intent": "thanks_closing", "intents": [], "action": "answer", "inherits_supported": True,
                        "inherits_issue": None})
    assert (s["primary_ok"], s["required_hit"], s["action_ok"], s["inherits_ok"]) == (False, 0, False, False)
    # 草稿输出还没有 inherits_issue 字段：跳过不计；没有草稿：主意图算错
    s = ev.score(case, {"intent": None, "intents": [], "action": None, "inherits_supported": False, "inherits_issue": None})
    assert s["primary_ok"] is False and s["inherits_ok"] is None
    # 状态卡里没有可沿用的问题（没有 prior_issue / 查件单 / 未结 case）：只能是 null，不评
    s = ev.score({"expect": case["expect"]}, {"intent": "delivery_delay", "inherits_supported": True, "inherits_issue": None})
    assert s["inherits_ok"] is None
    # 有未结 case 也算卡片里有问题；期望不沿用（换话题），模型填了 null → 对
    s = ev.score({"expect": {**case["expect"], "inherits_issue": False}, "context": {"case": {"case_type": "RETURN"}}},
                 {"intent": "delivery_delay", "inherits_supported": True, "inherits_issue": None})
    assert s["inherits_ok"] is True
    # 期望里没写 action / inherits 为 null：都不评
    s = ev.score({"expect": {"intent": ["other"], "inherits_issue": None}, "prior_issue": {"intent": "other", "summary_zh": "x"}},
                 {"intent": "other", "inherits_supported": True, "inherits_issue": "m1"})
    assert s["action_ok"] is None and s["inherits_ok"] is None and s["primary_ok"]


def test_aggregate_and_failures(ev):
    def row(cid, tags, **sc):
        base = {"primary_ok": True, "required_total": 1, "required_hit": 1, "action_ok": None, "inherits_ok": None}
        return {"case_id": cid, "tags": tags, "score": {**base, **sc}}

    rows = [row("A", ["short_followup"], inherits_ok=True), row("B", ["short_followup", "multi_issue"], primary_ok=False,
                                                                 required_total=2, required_hit=1, inherits_ok=False),
            row("C", ["multi_issue"], action_ok=True)]
    agg = ev.aggregate(rows)
    o = agg["overall"]
    assert (o["cases"], o["primary_acc"], o["required_hit_rate"], o["inherits_acc"], o["inherits_n"]) == (3, 0.667, 0.75, 0.5, 2)
    assert o["action_acc"] == 1.0 and o["action_n"] == 1
    assert agg["by_tag"]["short_followup"]["cases"] == 2 and agg["by_tag"]["multi_issue"]["primary_acc"] == 0.5
    assert agg["by_tag"]["multi_issue"]["inherits_acc"] == 0.0
    assert [r["case_id"] for r in ev.failures(rows)] == ["B"]
    assert ev.aggregate([row("D", [], action_ok=None)])["overall"]["action_acc"] is None  # 没有可评的：None，不报错


def test_fixture_and_dry_run(ev, capsys):
    data = json.loads(ev.CASES_FILE.read_text(encoding="utf-8"))
    cases = [dict(c) for c in data["cases"][:3]]
    fx = ev.build_fixture(data, cases, datetime(2026, 11, 9, 12, tzinfo=timezone.utc))
    assert len(fx["listings"]) == 3 and all(x["store_id"] == ev.EVAL_STORE for x in fx["listings"])
    assert {o["order_id"] for o in fx["orders"]} <= {c["_order_id"] for c in cases}
    assert ev.main([]) == 0  # 默认 dry-run：只校验和估算，不调模型
    out = capsys.readouterr().out
    assert "格式校验通过" in out and "预计调用模型" in out
    assert ev.main(["--cases", "NOPE"]) == 1


def test_prior_issue_seeds_state_card(ev, app_env):
    """回放前把 prior_issue 写进会话状态卡，起草时模型能看到可沿用的问题（不调模型，只检查重建的会话）。"""
    from app import conversation_state
    from app.db import session_scope
    from app.models import Conversation, Store
    from sqlalchemy import select

    data = json.loads(ev.CASES_FILE.read_text(encoding="utf-8"))
    case = dict(next(c for c in data["cases"] if c["case_id"] == "C01"))
    now = datetime.now(timezone.utc)
    ev.build_fixture(data, [case], now)
    with session_scope() as db:
        db.add(Store(id=ev.EVAL_STORE, name="评测店铺", timezone="Europe/Berlin", default_marketplace_id="EBAY_DE"))
    ev.queue_case(case, now)
    with session_scope() as db:
        conv = db.scalar(select(Conversation).where(Conversation.store_id == ev.EVAL_STORE))
        card = conversation_state.current(db, conv)
        assert [i["id"] for i in card["issues"]] == ["m1"] and card["issues"][0]["intent"] == "shipping_status"
        assert conversation_state.issue(card, "m1") is not None
    assert ev.inherits_supported() in (True, False)
