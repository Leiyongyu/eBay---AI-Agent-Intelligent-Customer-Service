"""可观测性 B7：trace_id 贯穿请求 / 起草 / 发送，结构化 JSON 日志，按追踪号查记录。全部假模型，不碰外部系统。"""
import json
import logging
import uuid

from sqlalchemy import select

from app import alerts
from app.db import session_scope
from app.models import AgentRun, AuditEvent, LlmUsage, OutboundMessage, ToolCall
from app.observability import JsonFormatter, TraceFilter, bind, current_trace_id, setup_logging, trace_scope
from app.worker import worker
from test_pipeline import detail, draft_json, send_buyer, tool_call, use_fake_tool_llm


def test_http_trace_header(client):
    r = client.get("/api/health")
    tid = r.headers["X-Trace-Id"]
    assert len(tid) == 16
    assert client.get("/api/health", headers={"X-Trace-Id": "abc-123_X"}).headers["X-Trace-Id"] == "abc-123_X"
    assert client.get("/api/health", headers={"X-Request-ID": "req-9"}).headers["X-Trace-Id"] == "req-9"
    bad = client.get("/api/health", headers={"X-Trace-Id": "bad id; drop"}).headers["X-Trace-Id"]
    assert bad != "bad id; drop" and len(bad) == 16  # 不合法的不沿用（防日志注入）
    assert client.get("/internal/no-such-api").headers.get("X-Trace-Id")  # 404 也带


def test_trace_through_draft_and_send(client):
    use_fake_tool_llm(tool_call("get_package_status", {"order_id": "26-10001-US"}), draft_json())
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    d = detail(client, sent["conversation_id"])
    with session_scope() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.job_id == d["job"]["id"]))
        assert run.trace_id and len(run.trace_id) == 16
        tools = {t.trace_id for t in db.scalars(select(ToolCall).where(ToolCall.job_id == d["job"]["id"]))}
        usage = {u.trace_id for u in db.scalars(select(LlmUsage).where(LlmUsage.job_id == d["job"]["id"]))}
    assert tools == {run.trace_id} and usage == {run.trace_id}  # 起草时的工具调用、模型调用都是这次运行的追踪号

    draft = d["drafts"][0]
    r = client.post(f"/internal/drafts/{draft['id']}/approve", headers={"X-Trace-Id": "approve-trace-1"},
                    json={"text": draft["text"], "expected_conversation_version": d["conversation"]["row_version"],
                          "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 202 and r.headers["X-Trace-Id"] == "approve-trace-1"
    worker.run_once()
    with session_scope() as db:
        ob = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_id == sent["conversation_id"]))
        assert ob.trace_id == "approve-trace-1" and ob.state == "SENT"
        acts = [a.action for a in db.scalars(select(AuditEvent).where(AuditEvent.trace_id == "approve-trace-1"))]
    assert len(acts) >= 2  # 批准时的审计 + Worker 发出时的审计，同一个追踪号

    t = client.get("/api/system/trace/approve-trace-1").json()
    assert [o["id"] for o in t["outbound"]] == [ob.id] and len(t["audit_events"]) == len(acts)
    t = client.get(f"/api/system/trace/{run.trace_id}").json()
    assert t["agent_runs"][0]["job_id"] == d["job"]["id"] and t["tool_calls"] and t["llm_usage"]
    assert client.get("/api/system/trace/bad%20id").status_code == 422


def test_trace_scope_and_bind():
    assert current_trace_id() is None
    with trace_scope(store_id="S1") as a:
        assert current_trace_id() == a
        with trace_scope() as b:  # 不 new：沿用外层
            assert b == a
        with trace_scope(new=True, job_id=5) as c:
            assert c != a
        with trace_scope("given-1") as g:
            assert g == "given-1"
        assert current_trace_id() == a
    assert current_trace_id() is None


def _record(logger: str, msg: str, level=logging.INFO, exc=None) -> logging.LogRecord:
    rec = logging.LogRecord(logger, level, __file__, 1, msg, (), exc)
    TraceFilter().filter(rec)
    return rec


def test_json_formatter_fields():
    with trace_scope("t-json-1", store_id="STORE_A"), bind(conversation_id=12, job_id=34):
        rec = _record("app.worker", "任务完成")
    out = json.loads(JsonFormatter().format(rec))
    assert out["trace_id"] == "t-json-1" and out["store_id"] == "STORE_A" and out["conversation_id"] == 12
    assert out["job_id"] == 34 and out["msg"] == "任务完成" and out["level"] == "INFO" and out["ts"].endswith("Z")
    rec = _record("x", "plain")
    out = json.loads(JsonFormatter().format(rec))
    assert out["trace_id"] is None and "store_id" not in out
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record("x", "failed", logging.ERROR, sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(rec))["exc"]


def test_setup_logging_json_file(tmp_path):
    from types import SimpleNamespace

    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    path = tmp_path / "logs" / "app.log"
    try:
        setup_logging(SimpleNamespace(log_format="json", log_file=str(path), log_file_max_mb=1, log_file_backups=1,
                                      log_level="INFO"))
        with trace_scope("t-file-1"):
            logging.getLogger("app.test").info("写到文件")
        for h in root.handlers:
            h.flush()
        line = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert line["trace_id"] == "t-file-1" and line["msg"] == "写到文件"
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)


def test_alert_carries_trace(app_env):
    alerts.reset()
    with trace_scope("t-alert-1"):
        alerts.notify("test", "标题", "内容", key="k-trace")
    assert alerts.sent[-1]["trace_id"] == "t-alert-1"
    card = alerts._card("标题", "内容", "warning", "t-alert-1")
    assert card["elements"][-1]["elements"][0]["content"] == "追踪号 t-alert-1"
    alerts.reset()
