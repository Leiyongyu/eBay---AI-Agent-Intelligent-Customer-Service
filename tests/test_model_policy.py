"""模型链、熔断、分级路由、预算护栏、语义缓存、前缀缓存（产品经理第 2 批 A 块）。

全部用假模型 / 假时钟，不调真实模型、不发飞书（告警开关默认关，只记录）。
"""
from datetime import datetime, timedelta

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app import alerts
from app.config import get_settings
from app.db import session_scope, utcnow
from app.models import AuditEvent, Conversation, Draft, LlmUsage, ReplyCacheEntry, ReplyJob
from app.providers import model_chain as mc
from app.providers.chat import ChatModelProvider, _usage, set_chat_provider_override
from app.worker import worker
from test_pipeline import FakeToolModel, detail, draft_json, send_buyer


@pytest.fixture(autouse=True)
def _clean_breaker():
    mc.breaker.reset()
    mc.breaker.clock = None
    alerts.reset()
    yield
    mc.breaker.reset()
    mc.breaker.clock = None
    alerts.reset()


def _cfg(monkeypatch, **kw):
    s = get_settings()
    base = {"llm_provider": "openai_compatible", "llm_chat_model": "m-main", "llm_api_key": "k",
            "llm_base_url": "https://example.invalid/v1"}
    for k, v in {**base, **kw}.items():
        monkeypatch.setattr(s, k, v, raising=False)
    return s


class Boom(Exception):
    pass


class APITimeoutError(Exception):
    pass


# ------------------------------------------------------------------ 模型链配置
def test_chain_names_backward_compatible(app_env, monkeypatch):
    s = _cfg(monkeypatch)
    assert mc.chain_names(s, "chat") == ["m-main"]  # 只配单个 LLM_CHAT_MODEL：链上就它一个
    assert mc.chain_names(s, "light") == ["m-main"]  # 没配轻量档：同主力档
    monkeypatch.setattr(s, "llm_translate_model", "", raising=False)
    assert mc.chain_names(s, "translate") == ["m-main"]

    s = _cfg(monkeypatch, llm_chat_models="a, b，a ,c", llm_translate_models="t1,t2", llm_light_models="l1")
    assert mc.chain_names(s, "chat") == ["a", "b", "c"]  # 去重、中文逗号也行
    assert mc.chain_names(s, "translate") == ["t1", "t2"]
    assert mc.chain_names(s, "light") == ["l1"]


def test_chain_specs_per_model_options(app_env, monkeypatch):
    s = _cfg(monkeypatch, llm_chat_models="a,b", llm_extra_body='{"enable_thinking": true}',
             llm_model_options='{"b": {"extra_body": {"enable_thinking": false}, "api_key": "kb", "base_url": "https://b/v1"}}')
    a, b = mc.chain_specs(s, "chat")
    assert a.api_key == "k" and a.base_url == "https://example.invalid/v1"
    assert b.extra_body == {"enable_thinking": False} and b.api_key == "kb" and b.base_url == "https://b/v1"
    monkeypatch.setattr(s, "llm_model_options", "{坏 JSON", raising=False)
    assert [x.name for x in mc.chain_specs(s, "chat")] == ["a", "b"]  # 写错了不影响链本身


def test_classify():
    assert mc.classify(Boom("Error code: 403 - AllocationQuota.FreeTierOnly")) == "quota"
    assert mc.classify(Boom("Arrearage: 账户欠费")) == "quota"
    assert mc.classify(APITimeoutError("Request timed out.")) == "timeout"
    assert mc.classify(Boom("Error code: 500 internal")) == "error"


# ------------------------------------------------------------------ 熔断
class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 15, 8, 0)

    def __call__(self):
        return self.t


def test_breaker_switches_and_half_open(app_env, monkeypatch):
    s = _cfg(monkeypatch, llm_chat_models="a,b")
    clock = Clock()
    mc.breaker.clock = clock
    specs = mc.chain_specs(s, "chat")
    calls = []

    def call(spec):
        calls.append(spec.name)
        if spec.name == "a" and state["a_down"]:
            raise Boom("500 internal")
        return f"ok-{spec.name}"

    state = {"a_down": True}
    # 普通错误：前两次失败仍会先试 a（未熔断），每次都切到 b 拿到结果
    for _ in range(2):
        assert mc.run_chain(specs, call, s) == ("ok-b", "b")
    assert calls == ["a", "b", "a", "b"]
    # 第 3 次连续失败：熔断 10 分钟
    assert mc.run_chain(specs, call, s) == ("ok-b", "b")
    snap = {x["model"]: x for x in mc.breaker.snapshot()}
    assert snap["a"]["state"] == "open" and snap["a"]["kind"] == "error" and snap["a"]["consecutive_failures"] == 3
    calls.clear()
    assert mc.run_chain(specs, call, s) == ("ok-b", "b")
    assert calls == ["b"]  # 冷却中直接跳过 a

    # 冷却到期：半开，放一次试探；a 已恢复 → 关闭熔断
    clock.t += timedelta(minutes=10)
    state["a_down"] = False
    calls.clear()
    assert mc.run_chain(specs, call, s) == ("ok-a", "a")
    assert {x["model"]: x for x in mc.breaker.snapshot()}["a"]["state"] == "closed"

    with session_scope() as db:
        actions = [e.action for e in db.query(AuditEvent).filter(AuditEvent.actor == "model_router").order_by(AuditEvent.id)]
    assert actions == ["MODEL_CIRCUIT_OPEN", "MODEL_CIRCUIT_CLOSED"]
    assert [a["title"] for a in alerts.sent] == ["模型熔断：a", "模型恢复：a"]
    assert not any(a["delivered"] for a in alerts.sent)  # 告警开关默认关：只记录，不发飞书


def test_breaker_half_open_probe_fails_reopens(app_env, monkeypatch):
    s = _cfg(monkeypatch, llm_chat_models="a,b")
    clock = Clock()
    mc.breaker.clock = clock
    specs = mc.chain_specs(s, "chat")

    def call(spec):
        if spec.name == "a":
            raise APITimeoutError("Request timed out.")
        return "ok"

    assert mc.run_chain(specs, call, s)[1] == "b"
    a = {x["model"]: x for x in mc.breaker.snapshot()}["a"]
    assert a["state"] == "open" and a["kind"] == "timeout"  # 超时：一次就熔断
    clock.t += timedelta(minutes=11)
    assert mc.run_chain(specs, call, s)[1] == "b"  # 试探又失败：重新冷却
    a = {x["model"]: x for x in mc.breaker.snapshot()}["a"]
    assert a["state"] == "open" and a["open_until"].startswith("2026-11-15T08:21")


def test_quota_cools_six_hours_and_whole_chain_down(app_env, monkeypatch):
    s = _cfg(monkeypatch, llm_chat_models="a,b")
    clock = Clock()
    mc.breaker.clock = clock
    specs = mc.chain_specs(s, "chat")

    def call(spec):
        raise Boom("Error code: 403 - AllocationQuota.FreeTierOnly")

    with pytest.raises(mc.ChainUnavailable, match="都失败了"):
        mc.run_chain(specs, call, s)
    snap = {x["model"]: x for x in mc.breaker.snapshot()}
    assert snap["a"]["kind"] == snap["b"]["kind"] == "quota"
    assert snap["a"]["open_until"].startswith("2026-11-15T14:00")
    with pytest.raises(mc.ChainUnavailable, match="都不可用"):
        mc.run_chain(specs, call, s)  # 都在冷却：不再调用
    clock.t += timedelta(hours=5, minutes=59)
    with pytest.raises(mc.ChainUnavailable, match="都不可用"):
        mc.run_chain(specs, call, s)


# ------------------------------------------------------------------ 接到起草流程
class FailingModel(GenericFakeChatModel):
    error: str = "Error code: 403 - AllocationQuota.FreeTierOnly"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *a, **kw):
        raise Boom(self.error)


def _chain_provider(monkeypatch, models: dict):
    s = get_settings()
    monkeypatch.setattr(ChatModelProvider, "_build", staticmethod(lambda settings, spec=None, *a, **kw: models[spec.name]))
    p = ChatModelProvider(s)
    set_chat_provider_override(p)
    return p


def test_draft_falls_back_to_next_model(client, monkeypatch):
    _cfg(monkeypatch, llm_chat_models="bad,good")
    good = FakeToolModel(messages=iter([AIMessage(content=draft_json())]))
    _chain_provider(monkeypatch, {"bad": FailingModel(messages=iter([])), "good": good})
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["drafts"], d["job"]
    assert "LLM_ERROR" not in d["drafts"][0]["review_reasons"]
    with session_scope() as db:
        models = {u.model for u in db.query(LlmUsage).filter(LlmUsage.purpose.in_(("reply", "repair")))}
    assert models == {"good"}  # 用量记实际用的模型
    st = client.get("/api/system/status").json()["models"]
    chat = {m["model"]: m for m in st["chains"]["chat"]}
    assert chat["bad"]["state"] == "open" and chat["bad"]["kind"] == "quota" and chat["good"]["state"] == "closed"


def test_whole_chain_down_hands_off(client, monkeypatch):
    _cfg(monkeypatch, llm_chat_models="x,y")
    _chain_provider(monkeypatch, {"x": FailingModel(messages=iter([])), "y": FailingModel(messages=iter([]), error="500 boom")})
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "REVIEW_REQUIRED" and "LLM_ERROR" in d["job"]["decision_reasons"]
    assert d["drafts"] == []


# ------------------------------------------------------------------ 分级路由
def _agent_draft(conv_id: int, intents: list[str], risk="L0"):
    with session_scope() as db:
        job = db.query(ReplyJob).filter(ReplyJob.conversation_id == conv_id).order_by(ReplyJob.id.desc()).first()
        conv = db.get(Conversation, conv_id)
        n = db.query(Draft).filter(Draft.job_id == job.id).count()
        db.add(Draft(job_id=job.id, run_generation=99, version=100 + n, author="agent", text="x", intents=intents,
                     intent=intents[0], risk_level=risk, conversation_row_version=conv.row_version))


def test_route_tier(client, monkeypatch):
    from app.agent.model_policy import route_tier

    s = _cfg(monkeypatch)
    sent = send_buyer(client, "Hello")
    cid = sent["conversation_id"]
    with session_scope() as db:
        assert route_tier(db, s, cid)[0] == "main"  # 默认全部主力档
    _cfg(monkeypatch, llm_light_models="m-light", llm_light_intents="greeting,shipping_status")
    with session_scope() as db:
        assert route_tier(db, s, cid) == ("main", "还没有判断过意图")
    _agent_draft(cid, ["shipping_status"])
    with session_scope() as db:
        assert route_tier(db, s, cid)[0] == "light"
        assert route_tier(db, s, cid, has_attachments=True)[0] == "main"
        assert route_tier(db, s, cid, open_flags={"INR"})[0] == "main"
        db.get(Conversation, cid).aftersales_status = "PENDING"
    with session_scope() as db:
        assert route_tier(db, s, cid) == ("main", "会话已转售后")
        db.get(Conversation, cid).aftersales_status = None
    _agent_draft(cid, ["return_request"])
    with session_scope() as db:
        assert route_tier(db, s, cid)[0] == "main"
    _agent_draft(cid, ["greeting"], risk="L2")
    with session_scope() as db:
        assert route_tier(db, s, cid)[1] == "上一轮风险 L2"


def test_light_tier_uses_light_chain(client, monkeypatch):
    _cfg(monkeypatch, llm_chat_models="main1", llm_light_models="light1", llm_light_intents="shipping_status")
    main = FakeToolModel(messages=iter([AIMessage(content=draft_json())] * 1))
    light = FakeToolModel(messages=iter([AIMessage(content=draft_json())]))
    _chain_provider(monkeypatch, {"main1": main, "light1": light})
    sent = send_buyer(client, "Where is my order?")
    _agent_draft(sent["conversation_id"], ["shipping_status"])
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    compose = next(s for s in d["steps"] if s["node"] == "compose_reply")
    # 物流查询会话：prefetch 发现包裹问题时 open_flags 可能不空 → 主力档；否则轻量档。两种都要记下原因
    assert compose["detail"]["tier"] in ("light", "main") and compose["detail"]["tier_reason"]
    with session_scope() as db:
        used = {u.model for u in db.query(LlmUsage).filter(LlmUsage.purpose.in_(("reply", "repair")))}
    assert used == {"light1" if compose["detail"]["tier"] == "light" else "main1"}


# ------------------------------------------------------------------ 预算护栏
def _spend(store: str, tokens: int):
    with session_scope() as db:
        db.add(LlmUsage(purpose="reply", provider="openai_compatible", model="m", store_id=store,
                        input_tokens=tokens, output_tokens=0, total_tokens=tokens, created_at=utcnow()))


def test_budget_status_and_store_override(app_env, monkeypatch):
    from app.agent import model_policy as mp

    s = _cfg(monkeypatch)
    with session_scope() as db:
        assert mp.budget_status(db, s, "STORE_A")["state"] == "ok"  # 0 = 不限
    _spend("STORE_A", 1100)
    _cfg(monkeypatch, llm_daily_token_budget=1000)
    with session_scope() as db:
        st = mp.budget_status(db, s, "STORE_A")
        assert st["state"] == "over" and st["used"] == 1100
        mp.budget_alert(st)
        mp.budget_alert(st)  # 同一天同一状态只告警一次
    assert len(alerts.sent) == 1 and alerts.sent[0]["kind"] == "budget"
    _cfg(monkeypatch, llm_store_token_budgets='{"STORE_A": 900}')
    with session_scope() as db:
        assert mp.budget_status(db, s, "STORE_A")["state"] == "blocked"  # 1100 / 900 ≥ 1.2
        assert mp.budget_status(db, s, "STORE_B")["budget"] == 1000


def test_budget_blocked_skips_drafting(client, monkeypatch):
    _cfg(monkeypatch, llm_daily_token_budget=100)
    model = FakeToolModel(messages=iter([AIMessage(content=draft_json())]))
    set_chat_provider_override(ChatModelProvider(get_settings(), model=model))
    _spend("STORE_A", 500)
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["drafts"] == [] and "TOKEN_BUDGET_EXCEEDED" in d["job"]["decision_reasons"]
    assert any(a["kind"] == "budget" for a in alerts.sent)
    u = client.get("/api/usage/summary").json()
    st = {b["store_id"]: b for b in u["budget"]["stores"]}
    assert st["STORE_A"]["state"] == "blocked"


# ------------------------------------------------------------------ 语义缓存
def _vec(q: str):
    return [1.0, 0.0, 0.0] if "invoice" in q.lower() else [0.0, 1.0, 0.0]


def test_cache_store_and_lookup(app_env, monkeypatch):
    from app.agent import model_policy as mp

    s = _cfg(monkeypatch)
    draft = Draft(id=7, intents=["invoice_vat"], language="en", risk_level="L0", evidence_ids=["kb:CS-INV-001"],
                  knowledge_versions=["CS-INV-001@3"], text_zh="中文")
    job = ReplyJob(store_id="STORE_A")
    with session_scope() as db:
        assert mp.cache_store(db, s, draft, job, "We can issue an invoice.", "Can I get an invoice?", _vec) is False  # 默认关
    _cfg(monkeypatch, reply_cache_enabled=True)
    with session_scope() as db:
        assert mp.cache_store(db, s, Draft(intents=["shipping_status"], risk_level="L0"), job, "x", "q", _vec) is False
        assert mp.cache_store(db, s, Draft(intents=["invoice_vat"], risk_level="L0", evidence_ids=["order:1"]),
                              job, "x", "q", _vec) is False  # 依赖订单的不缓存
        assert mp.cache_store(db, s, draft, job, "We can issue an invoice.", "Can I get an invoice?", _vec) is True
    with session_scope() as db:
        hit, score = mp.cache_lookup(db, s, "STORE_A", "I need an invoice please", ["CS-INV-001@3"], _vec)
        assert hit is not None and score > 0.99 and hit.output["reason_code"] == "FROM_CACHE"
        assert mp.cache_lookup(db, s, "STORE_A", "Where is my parcel", ["CS-INV-001@3"], _vec)[0] is None
        assert mp.cache_lookup(db, s, "STORE_A", "invoice", ["CS-INV-001@4"], _vec)[0] is None  # 知识改版本：不命中
        assert mp.cache_lookup(db, s, "STORE_B", "invoice", ["CS-INV-001@3"], _vec)[0] is None  # 别的店铺
    with session_scope() as db:
        e = db.query(ReplyCacheEntry).one()
        assert e.hits == 1 and mp.cache_stats(db, utcnow(), ["STORE_A"]) == {"entries": 1, "hits": 1}


# ------------------------------------------------------------------ 前缀缓存
def test_prompt_cache_marks_system_prompt(app_env, monkeypatch):
    s = _cfg(monkeypatch)
    p = ChatModelProvider(s, model=GenericFakeChatModel(messages=iter([])))
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    assert p._cache(msgs) is msgs  # 默认关
    monkeypatch.setattr(s, "llm_prompt_cache", True)
    out = p._cache(msgs)
    assert out[0].content == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert out[1] is msgs[1]
    msg = AIMessage(content="x", usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
                                                 "input_token_details": {"cache_read": 80}})
    assert _usage(msg)["cached_tokens"] == 80


def test_usage_summary_prompt_cache(client):
    with session_scope() as db:
        db.add(LlmUsage(purpose="reply", provider="openai_compatible", model="m", store_id="STORE_A", input_tokens=200,
                        output_tokens=10, total_tokens=210, cached_tokens=150, created_at=utcnow()))
    u = client.get("/api/usage/summary").json()
    assert u["prompt_cache"] == {"cached_tokens": 150, "input_tokens": 200, "hit_ratio": 0.75}
    assert u["reply_cache"]["enabled"] is False and u["budget"]["stores"] == []
