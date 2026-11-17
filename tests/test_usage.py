"""模型调用用量：Agent 生成时自动记录，统计接口按用途/模型/店铺/会话/日期汇总。"""
from test_pipeline import draft_json, send_buyer, use_fake_llm


def test_reply_generation_is_recorded(client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import LlmUsage
    from app.worker import worker

    use_fake_llm(draft_json())
    sent = send_buyer(client, "Where is my order?")
    worker.run_once()
    with session_scope() as db:
        rows = db.scalars(select(LlmUsage)).all()
        assert [(r.purpose, r.store_id, r.conversation_id, r.job_id, r.ok) for r in rows] == [
            ("reply", "STORE_A", sent["conversation_id"], sent["job_id"], True)]


def test_summary_groups_and_filters(client):
    from app import usage
    from app.db import session_scope

    conv = send_buyer(client, "Hello")["conversation_id"]
    with session_scope() as db:
        usage.record(db, "reply", provider="openai_compatible", model="qwen", store_id="STORE_A", conversation_id=conv,
                     usage={"input_tokens": 1000, "output_tokens": 200}, duration_ms=3000)
        usage.record(db, "reply", provider="codex_cli", model="账号默认模型", store_id="STORE_A", conversation_id=conv,
                     usage={"total_tokens": 16000, "seconds": 33}, duration_ms=33000)
        usage.record(db, "repair", provider="codex_cli", model="账号默认模型", store_id="STORE_B",
                     usage=usage.usage_delta({"total_tokens": 20000}, {"total_tokens": 16000}), ok=False, error="bad json")
        usage.record(db, "embedding_query", provider="openai_compatible", model="emb", store_id="STORE_A",
                     estimated_tokens=30)
        usage.record(db, "translate", provider="codex_cli", model="账号默认模型", usage={"total_tokens": 500})

    s = client.get("/api/usage/summary", params={"days": 1}).json()
    t = s["totals"]
    assert (t["calls"], t["failed"], t["total_tokens"], t["estimated_tokens"]) == (5, 1, 1200 + 16000 + 4000 + 30 + 500, 30)
    assert t["input_tokens"] == 1030 and t["output_tokens"] == 200  # 历史 Codex 记录只有合计，不计入输入/输出
    assert len(s["by_day"]) == 1 and s["by_day"][0]["total_tokens"] == t["total_tokens"]
    assert [p["purpose"] for p in s["by_purpose"]] == ["reply", "repair", "translate", "embedding_query"]
    assert s["by_purpose"][0] == {**s["by_purpose"][0], "calls": 2, "total_tokens": 17200, "avg_tokens": 8600,
                                  "avg_seconds": 18.0, "label": "回复生成"}
    assert s["by_model"][0]["provider_label"] == "ChatGPT 账号（Codex，已停用）" and s["by_model"][0]["total_tokens"] == 20500
    assert {x["store_id"]: x["total_tokens"] for x in s["by_store"]} == {"STORE_A": 17230, "STORE_B": 4000, None: 500}
    assert s["top_conversations"][0]["conversation_id"] == conv and s["top_conversations"][0]["total_tokens"] == 17200
    assert s["recent"][0]["purpose"] == "translate" and s["recent"][-1]["model"] == "qwen"

    a = client.get("/api/usage/summary", params={"days": 1, "store_id": "STORE_B"}).json()["totals"]
    assert (a["calls"], a["total_tokens"], a["failed"]) == (1, 4000, 1)
    assert client.get("/api/usage/summary", params={"start": "2026-01-02", "end": "2026-01-01"}).status_code == 422
