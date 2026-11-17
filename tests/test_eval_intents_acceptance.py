"""验收（测试工程师，2026-11-09）：多轮意图评测脚本的安全性——默认不调模型、--live 要确认、
用假模型完整跑一遍（禁止一切外网连接）、评测库独立、关掉的开关在初始化后不能被 .env 改回来。"""
import importlib.util
import json
import socket
from pathlib import Path

import pytest

from app import config
from test_pipeline import draft_json, use_fake_llm

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def ev(monkeypatch):
    from app import db as dbmod
    from app import sources

    # setup_eval_env 会全局替换业务数据源、切数据库引擎（脚本是一次性进程，没问题）；测试结束后还原，免得影响后面的用例
    monkeypatch.setattr(sources, "get_business_source", sources.get_business_source)
    spec = importlib.util.spec_from_file_location("eval_intents_acc", ROOT / "scripts" / "eval_intents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    dbmod.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture()
def no_network(monkeypatch):
    """除本机外一律不许连网：谷仓、17TRACK、飞书、eBay、翻译、真实大模型都会在这里被拦下。"""
    tried = []
    real = socket.socket.connect

    def guard(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost"):
            tried.append(host)
            raise OSError(f"验收测试禁止连网：{host}")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guard)
    return tried


def test_default_is_dry_run_and_never_builds_model(ev, app_env, monkeypatch, capsys):
    from app.providers import chat

    monkeypatch.setattr(chat.ChatModelProvider, "_build", staticmethod(lambda *a, **k: pytest.fail("dry-run 建了模型")))
    assert ev.main([]) == 0 and ev.main(["--dry-run", "--cases", "C01,C02"]) == 0
    out = capsys.readouterr().out
    assert "dry-run，没有调用模型" in out and "用例 2 组" in out


def test_live_without_yes_asks_and_can_cancel(ev, app_env, monkeypatch, capsys):
    s = config.get_settings()
    monkeypatch.setattr(type(s), "llm_enabled", property(lambda self: True), raising=False)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "no")
    monkeypatch.setattr(ev, "run_live", lambda *a, **k: pytest.fail("没确认就开始跑了"))
    assert ev.main(["--live", "--cases", "C01"]) == 1
    assert asked and "确认开始请输入 yes" in asked[0]
    assert "已取消" in capsys.readouterr().out


def test_fake_model_full_replay_isolated(ev, app_env, no_network, tmp_path, monkeypatch):
    """假模型跑 5 组（含查件单、未结 case、短追问、换话题）：评测库是独立 SQLite、没有任何外网连接、
    没有发给买家的消息、报告能生成，新字段能被统计。"""
    from sqlalchemy import func, select

    from app import db as dbmod

    data = json.loads(ev.CASES_FILE.read_text(encoding="utf-8"))
    want = ["C01", "C06", "C12", "C29", "C37"]
    cases = [dict(c) for c in data["cases"] if c["case_id"] in want]
    outs = []
    for c in cases:
        outs.append(draft_json(intent=c["expect"]["intent"][0], intents=c["expect"]["intent"][:1], claims=[],
                               intent_confidence="medium", inherits_issue=None))
    use_fake_llm(*outs * 3)
    prod_url = config.get_settings().effective_database_url
    rows = ev.run_live(data, cases, tmp_path, None)
    assert dbmod.get_engine().url.database.endswith("eval.db")  # 回放用的是评测库
    assert (tmp_path / "eval.db").exists() and prod_url not in str(dbmod.get_engine().url)
    assert no_network == []  # 没有任何外网连接

    from app.models import AftersalesCase, InvoiceRequest, LogisticsTicket, OutboundMessage, Message

    with dbmod.session_scope() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 0
        assert db.scalar(select(func.count(Message.id)).where(Message.direction == "OUT", Message.ebay_message_id.like("H-%").is_(False))) == 0
        assert db.scalar(select(func.count(AftersalesCase.id)).where(AftersalesCase.status.in_(("SENT", "REPLIED")))) == 0
        assert db.scalar(select(func.count(LogisticsTicket.id)).where(LogisticsTicket.submitted_at.is_not(None))) == 0
        assert db.scalar(select(func.count(InvoiceRequest.id))) == 0
    assert len(rows) == 5 and all(r["got"]["intent"] for r in rows)
    assert all(r["got"]["intent_confidence"] == "medium" for r in rows)
    md = ev.write_report(rows, {"run_id": "acc", "cases_file": "intent_cases.json", "version": data.get("version"),
                                "model": "fake", "knowledge": "不注入", "seconds": 1, "tokens": 0}, tmp_path)
    text = md.read_text(encoding="utf-8")
    assert "主意图准确率" in text and "intent_confidence（有）" in text and "按意图把握度" in text


# 2026-11-09 已修：setup_eval_env 改用 config.set_overrides 并在 bootstrap 后再盖一次（原来标 xfail 记录这个问题）
def test_switches_stay_off_after_setup(ev, app_env, tmp_path, monkeypatch, request):
    for k in ("FEISHU_CASE_ENABLED", "FEISHU_CASE_AUTO_SEND", "AUTO_TRANSLATE_BUYER_MESSAGES", "TRACK17_ENABLED",
              "LOGISTICS_TICKET_AUTO_DRAFT", "SHIP_NOTICE_ENABLED"):
        monkeypatch.setenv(k, "true")  # 模拟正式 .env 里这些是开着的
    config.get_settings.cache_clear()
    data = json.loads(ev.CASES_FILE.read_text(encoding="utf-8"))
    restore = ev.setup_eval_env(tmp_path, ev.build_fixture(data, [dict(data["cases"][0])], __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc)), None)
    request.addfinalizer(restore)  # 还原配置覆盖值和业务数据源，不影响后面的测试
    s = config.get_settings()
    on = {k: getattr(s, k) for k in ("feishu_case_enabled", "feishu_case_auto_send", "auto_translate_buyer_messages",
                                     "track17_enabled", "logistics_ticket_auto_draft", "ship_notice_enabled") if getattr(s, k)}
    assert on == {}
    assert "eval.db" in s.effective_database_url


def test_new_field_names_match_implementation(ev):
    from app.agent.schemas import ReplyDraftOutput
    from app.models import Draft

    assert set(ev.NEW_FIELDS) == {"intent_confidence", "intent_basis", "inherits_issue"}
    for f in ev.NEW_FIELDS:
        assert f in ReplyDraftOutput.model_fields and hasattr(Draft, f)
    assert ev.inherits_supported() is True
