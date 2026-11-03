"""端到端：买家来信（直接走接入代码写库，与客户测试消息系统相同）→ Worker 运行固定流程 → 草稿/闸门 → 审核批准 → 受控发送（mock 渠道）。

模型用 GenericFakeChatModel 注入，只验证编排、校验和落库，不代表真实模型效果。
"""
import json
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.config import get_settings
from app.providers.chat import ChatModelProvider, set_chat_provider_override
from app.worker import worker


def use_fake_llm(*outputs: str):
    """不支持工具调用的假模型：只走单次生成（只有程序预取的订单与售后）。"""
    model = GenericFakeChatModel(messages=iter([AIMessage(content=o) for o in outputs]))
    set_chat_provider_override(ChatModelProvider(get_settings(), model=model))


class FakeToolModel(GenericFakeChatModel):
    """支持原生工具调用的假模型：按顺序吐出预设消息（可以是带 tool_calls 的 AIMessage），并记下收到的工具定义。"""

    bound_tools: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append([t["function"]["name"] for t in tools])
        return self


def tool_call(name: str, args: dict | None = None, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


def use_fake_tool_llm(*outputs):
    """outputs：tool_call(...) 或最终 JSON 字符串。"""
    msgs = [o if isinstance(o, AIMessage) else AIMessage(content=o) for o in outputs]
    model = FakeToolModel(messages=iter(msgs), bound_tools=[])
    set_chat_provider_override(ChatModelProvider(get_settings(), model=model))
    return model


def draft_json(**overrides) -> str:
    base = {
        "intent": "shipping_status", "intents": ["shipping_status"], "language": "en", "action": "answer",
        "needs_human": False,
        "claims": [{"text": "Your parcel left the Phoenix facility.", "evidence_ids": ["package:PKG-10001-1"]}],
        "missing_facts": [], "conflicts": [],
        "reply_text": "Hi, your parcel is on its way with USPS and recently departed the Phoenix facility.",
        "reply_zh": "您好，您的包裹正由 USPS 运输，最近已离开凤凰城处理中心。",
        "reason_code": "verified_order_status",
    }
    base.update(overrides)
    return json.dumps(base)


def send_buyer(client, text, buyer="buyer_us_001", item="110000000001", skip=True, store="STORE_A", images=()):
    """模拟买家来信：主程序已不提供模拟接口，直接调用接入代码写库（client 参数保留以兼容调用方）。"""
    from app.db import session_scope
    from app.ingestion.service import get_or_create_conversation, ingest_buyer_message
    from app.media.service import add_uploaded_image
    from app.sources.fixture import FixtureSource

    marketplace, source = "UNKNOWN", "simulator"
    if item:
        listing = FixtureSource().get_listing(store, item)
        assert listing is not None, "该刊登不属于所选店铺"
        marketplace, source = listing["marketplace_id"], "listing"
    with session_scope() as db:
        conv = get_or_create_conversation(db, store, f"SIM-{store}-{buyer}-{item or 'general'}-{marketplace}",
                                          buyer, item, marketplace, source)
        result = ingest_buyer_message(db, conv=conv, ebay_message_id=f"SIM-IN-{uuid.uuid4().hex[:12]}", body=text,
                                      skip_merge_window=skip, actor="test")
        stored = [add_uploaded_image(db, result.message, i, name, data) for i, (name, data) in enumerate(images, 1)]
        return {"conversation_id": conv.id, "job_id": result.job.id if result.job else None,
                "cancelled_job_ids": list(result.cancelled_job_ids),
                "attachments": [{"id": a.id, "status": a.status, "reason": a.status_reason} for a in stored]}


def detail(client, conv_id):
    return client.get(f"/internal/conversations/{conv_id}").json()


def test_not_ready_then_manual_reply(client):
    sent = send_buyer(client, "Where is my order?")
    assert worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "REVIEW_REQUIRED"
    assert "AGENT_NOT_READY" in d["job"]["decision_reasons"]
    assert d["drafts"] == []
    tools = {t["tool_name"]: t["status"] for t in d["tool_calls"]}
    assert "get_item_details" not in tools and "get_listing_context" not in tools  # 商品详情由模型按需请求，不再预取
    assert tools["get_authorized_order_context"] == "FOUND" and tools["get_case_status"] in ("FOUND", "NOT_FOUND")
    # 买家问物流：程序固定先查包裹（断更天数、查不到转人工都靠这一步，不依赖模型想不想查）
    assert tools["get_package_status"] == "FOUND"

    r = client.post(f"/internal/jobs/{d['job']['id']}/manual-reply", json={
        "text": "Hi, your order is on the way.", "expected_conversation_version": d["conversation"]["row_version"],
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 202, r.text
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "SENT"
    assert d["messages"][-1]["role"] == "SELLER"


def test_draft_review_and_approve(client):
    # 模型先自己调用物流工具，再按查到的包裹作答（原生工具调用）
    model = use_fake_tool_llm(tool_call("get_package_status", {"order_id": "26-10001-US"}), draft_json())
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    draft = d["drafts"][0]
    assert d["job"]["state"] == "REVIEW_REQUIRED"
    assert draft["risk_level"] == "L0"
    assert draft["compliance"]["result"] == "PASS"
    # P2：只有闸门说明类原因（未开启自动发送等），没有草稿问题
    assert set(draft["review_reasons"]) <= {"AUTO_SEND_DISABLED", "STORE_AUTO_SEND_OFF", "INTENT_NOT_WHITELISTED",
                                            "NO_KB_EVIDENCE"}
    assert ("get_package_status", "FOUND") in [(t["tool_name"], t["status"]) for t in d["tool_calls"]]
    assert "get_item_details" in model.bound_tools[0]  # 会话关联了商品，才给商品详情工具
    compose = next(s for s in d["steps"] if s["node"] == "compose_reply")
    assert compose["detail"]["tool_rounds"] == 1 and compose["detail"]["tool_calls"][0]["tool"] == "get_package_status"

    key = uuid.uuid4().hex
    body = {"text": draft["text"], "expected_conversation_version": d["conversation"]["row_version"],
            "idempotency_key": key}
    r1 = client.post(f"/internal/drafts/{draft['id']}/approve", json=body)
    r2 = client.post(f"/internal/drafts/{draft['id']}/approve", json=body)  # 重复点击
    assert r1.status_code == 202 and r1.json()["intent_id"] == (r2.json().get("intent_id") or r1.json()["intent_id"])
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["state"] == "SENT"
    assert sum(m["role"] == "SELLER" for m in d["messages"]) == 1
    metrics = client.get("/internal/metrics/workbench").json()
    assert metrics["adoption_rate"] == 1.0


def test_repair_once_then_invalid_evidence(client):
    bad = "not json at all"
    fixed = draft_json(claims=[{"text": "x", "evidence_ids": ["order:FAKE-1"]}])
    use_fake_llm(bad, fixed)
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    nodes = [s["node"] for s in d["steps"]]
    assert "repair_output" in nodes
    assert "EVIDENCE_INVALID" in d["drafts"][0]["review_reasons"]


def test_open_case_raises_risk(client):
    use_fake_llm(draft_json(claims=[]))
    sent = send_buyer(client, "Is my sensor OK?", buyer="buyer_us_002", item="110000000003")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    assert d["job"]["risk_level"] == "L2"
    assert "OPEN_CASE" in d["job"]["decision_reasons"]


def test_merge_window_cancels_previous_job(client):
    first = send_buyer(client, "Hello", skip=False)
    second = send_buyer(client, "Also, where is my order?", skip=False)
    assert second["cancelled_job_ids"] == [first["job_id"]]
    d = detail(client, second["conversation_id"])
    assert len(d["sources"]) == 2
    assert worker.run_once() is False  # 合并窗口未结束，不处理


def test_compliance_block_on_approve(client):
    sent = send_buyer(client, "Can I email you?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    r = client.post(f"/internal/jobs/{d['job']['id']}/manual-reply", json={
        "text": "Sure, write to sales@example.com", "expected_conversation_version": d["conversation"]["row_version"],
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 422
    assert r.json()["detail"]["result"] == "BLOCK"


def test_system_message_triggers_handoff(client):
    sent = send_buyer(client, "Hi")
    from app.db import session_scope
    from app.ingestion.service import ingest_system_message
    from app.models import Conversation

    with session_scope() as db:
        conv = db.get(Conversation, sent["conversation_id"])
        ingest_system_message(db, conv=conv, ebay_message_id="SIM-SYS-1", body="eBay: The buyer has opened a return request.",
                              event_type="RETURN_OPENED")
        assert conv.handoff_state == "HUMAN"
    worker.run_once()
    assert "HUMAN_HANDOFF" in detail(client, sent["conversation_id"])["job"]["decision_reasons"]


def test_claim_conflict(client):
    sent = send_buyer(client, "Hi")
    cid = sent["conversation_id"]
    assert client.post(f"/internal/conversations/{cid}/claim", json={"action": "claim"}).status_code == 200
    from fastapi.testclient import TestClient

    from conftest import login, make_user

    make_user("someone-else", "CS", all_stores=True)
    other_client = TestClient(client.app)
    login(other_client, "someone-else")
    other = other_client.post(f"/internal/conversations/{cid}/claim", json={"action": "claim"})
    assert other.status_code == 409
