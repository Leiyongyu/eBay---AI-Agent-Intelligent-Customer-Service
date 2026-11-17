"""第一层规则评测进 CI：用 tests/fixtures/golden_sample.jsonl 的 10 条虚构样例，mock 回放当前整条起草流水线并断言。
不调模型、不连外部系统。有一条不过就说明程序侧规则（校验、闸门、合规、转人工、提示词拼装）出现了回归。"""
import pytest


@pytest.fixture()
def eval_env(app_env, monkeypatch):
    from app import config

    monkeypatch.setenv("KNOWLEDGE_MODE", "full_context")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_golden_sample_format():
    from app.evaluation import golden

    cases = golden.load(golden.SAMPLE_FILE)
    assert len(cases) == 10
    for c in cases:
        assert golden.validate(c) == [], c["case_id"]


def test_rules_eval_on_golden_sample(eval_env):
    from app.evaluation import golden, rules_eval

    report = rules_eval.run(golden.load(golden.SAMPLE_FILE))
    s = report["summary"]
    bad = {r["case_id"]: (r.get("error") or [k for k, v in r["checks"].items() if v is False], r.get("got"))
           for r in report["rows"] if r["ok"] is False}
    assert not bad, bad
    assert s["judged"] == 10 and s["pass_rate"] == 1.0
    assert s["by_mode"]["recorded"] == 1.0 and s["by_mode"]["standin"] == 1.0
    assert s["by_check"]["handoff"]["n"] == 10 and s["by_check"]["compliance"]["n"] == 10
