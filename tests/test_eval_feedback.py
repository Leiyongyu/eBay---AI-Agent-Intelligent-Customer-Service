"""人工修改回流（修改类型、每周改进建议、知识缺口导出）和置信度校准；全部规则，不调模型。"""
from datetime import date, datetime, timedelta

import pytest


def test_edit_types():
    from app.evaluation.feedback import edit_types

    d = "Hello, your parcel is with DHL, tracking 00340434161234567890."
    assert edit_types(d, d, 0.0) == []
    assert "fact" in edit_types(d, "Hello, your parcel is with UPS, tracking 1Z999AA10123456784.", 0.4)
    assert "length" in edit_types(d, d + " We checked with the carrier and will update you within the week as soon as we hear back.", 0.5)
    assert "tone" in edit_types(d, "Sorry for the delay! " + d, 0.2)
    assert "knowledge" in edit_types("Hello, thanks for your message.", "Hello, all our parts have a 1 year warranty.", 0.6)
    assert "language" in edit_types("Hello, your parcel is on its way.", "Hallo, Ihr Paket ist unterwegs und kommt bald.", 0.9)
    assert edit_types("abc def", "abc xyz", 0.3) == ["other"]


def _make(db, n, *, intent="shipping_status", conf="high", ratio=0.0, draft="Hello, your parcel is with DHL.", final=None,
          action=None, no_kb=False, top=None, days_ago=1, store="FB"):
    from app.db import utcnow
    from app.models import (AgentRun, AgentStep, Conversation, Draft, Message, ReplyJob, ReplyJobSource, ReviewFeedback,
                            SendIntent, Store)

    if db.get(Store, store) is None:
        db.add(Store(id=store, name="回流店", timezone="Europe/Berlin", default_marketplace_id="EBAY_DE"))
        db.flush()
    at = utcnow() - timedelta(days=days_ago)
    for i in range(n):
        conv = Conversation(store_id=store, ebay_conversation_id=f"C-{intent}-{conf}-{ratio}-{i}-{days_ago}-{top}",
                            buyer_key=f"buyer_max_{i}", marketplace_id="EBAY_DE")
        db.add(conv)
        db.flush()
        mid = f"M-{conv.id}"
        db.add(Message(store_id=store, ebay_message_id=mid, conversation_id=conv.id, conversation_seq=1, sender_role="BUYER",
                       direction="IN", body=f"Hi, I am buyer_max_{i}. How long is the warranty?", sent_at=at))
        job = ReplyJob(store_id=store, conversation_id=conv.id, anchor_source_message_id=mid, state="SENT", ready_at=at)
        db.add(job)
        db.flush()
        db.add(ReplyJobSource(store_id=store, source_message_id=mid, job_id=job.id))
        d = Draft(job_id=job.id, run_generation=1, version=1, author="agent", conversation_row_version=1, intent=intent,
                  intent_confidence=conf, text=draft, evidence_ids=[] if no_kb else ["kb:X@1"],
                  review_reasons=["NO_KB_EVIDENCE"] if no_kb else [], status="APPROVED", created_at=at)
        db.add(d)
        db.flush()
        run = AgentRun(job_id=job.id, run_generation=1, thread_id=f"t{job.id}", mode="fixed_pipeline")
        db.add(run)
        db.flush()
        db.add(AgentStep(run_id=run.id, node="load_knowledge", result_status="OK",
                         detail={"mode": "retrieval", "hits": [] if top is None else [{"rrf": top}]}))
        text = final if final is not None else draft
        db.add(SendIntent(job_id=job.id, draft_id=d.id, idempotency_key=f"k{job.id}", actor="rev", text=text,
                          content_hash="h", state="SENT"))
        db.add(ReviewFeedback(draft_id=d.id, job_id=job.id, reviewer="rev", edit_ratio=ratio,
                              action=action or ("APPROVE" if ratio == 0 else "EDIT_APPROVE"), created_at=at))
    db.flush()


def test_collect_stats_and_masking(app_env):
    from app.db import session_scope, utcnow
    from app.evaluation import feedback

    with session_scope() as db:
        _make(db, 3, ratio=0.0)
        _make(db, 2, ratio=0.6, draft="Hello, your parcel is with DHL.", final="Sorry! Your parcel is with UPS, 1Z999AA10123456784.")
        rows = feedback.collect(db, utcnow() - timedelta(days=7))
    assert len(rows) == 5
    st = feedback.edit_stats(rows)
    assert st["edited"] == 2 and st["edited_rate"] == 0.4 and st["by_type"]["fact"]["n"] == 2
    assert all("buyer_max_" not in " ".join(r["buyer"]) for r in rows)  # 买家用户名脱敏了


def test_weekly_suggestions_keep_decisions(app_env, client):
    from app.db import session_scope
    from app.evaluation import feedback

    with session_scope() as db:
        _make(db, 3, intent="item_spec", ratio=0.7, draft="Hello, thanks for your message.",
              final="Hello, all parts come with a 1 year warranty. Returns within 30 days.")
    r = client.post("/internal/eval/suggestions/generate").json()
    kn = next(x for x in r if x["edit_type"] == "knowledge")  # 补的内容里也有新数字，同一处修改也算“事实”
    assert kn["intent"] == "item_spec" and kn["count"] == 3 and kn["target"] == "knowledge"
    assert "知识缺口" in kn["suggestion"] and len(kn["examples"]) == 3
    assert {x["edit_type"] for x in r} >= {"knowledge", "fact"}
    sid = kn["id"]
    assert client.post(f"/internal/eval/suggestions/{sid}/decide", json={"status": "ACCEPTED", "note": "已加保修条款"}).status_code == 200
    again = client.post("/internal/eval/suggestions/generate").json()
    same = next(s for s in again if s["id"] == sid)
    assert same["status"] == "ACCEPTED" and same["note"] == "已加保修条款"  # 重新生成保留人工确认
    assert client.get("/internal/eval/suggestions?status=ACCEPTED").json()[0]["id"] == sid
    assert client.post(f"/internal/eval/suggestions/{sid}/decide", json={"status": "MAYBE"}).status_code == 422
    with session_scope() as db:
        assert feedback.weekly_due(db, datetime(2026, 11, 16, 2, 0)) is False  # 周一 3 点前
        assert feedback.weekly_due(db, datetime(2026, 11, 17, 4, 0)) is False  # 不是周一


def test_knowledge_gaps_and_export(app_env, client):
    import io

    from openpyxl import load_workbook

    from app.db import session_scope

    with session_scope() as db:
        _make(db, 2, intent="item_spec", ratio=0.8, no_kb=True, draft="Hello, thanks.",
              final="Hello, the warranty is 1 year from delivery.")
        _make(db, 1, intent="item_spec", ratio=0.8, no_kb=False, top=0.5, draft="Hello.", final="Hello, warranty 1 year.")  # 命中了
        _make(db, 1, intent="item_spec", ratio=0.1, no_kb=True)  # 没大改
    gaps = client.get("/internal/eval/knowledge-gaps").json()
    assert len(gaps) == 2 and gaps[0]["answer"] == "Hello, the warranty is 1 year from delivery."
    r = client.get("/internal/eval/knowledge-gaps/export")
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    head = [c.value for c in ws[1]]
    assert head[:5] == ["编号", "标题", "正文", "适用站点", "能否对买家说"] and ws.max_row == 3
    row = dict(zip(head, [c.value for c in ws[2]]))
    assert row["启用"] == "否" and row["能否对买家说"] == "可以" and row["主题"] == "商品" and row["适用店铺"] == "回流店"


def test_calibration_table(app_env, client):
    from app.db import session_scope

    with session_scope() as db:
        _make(db, 20, intent="shipping_status", conf="high", ratio=0.0)
        _make(db, 2, intent="shipping_status", conf="high", ratio=0.5, final="Totally different text here.")
        _make(db, 5, intent="fitment", conf="low", ratio=0.6, final="We need your VIN first.")
    t = client.get("/internal/eval/calibration?days=30").json()
    assert t["reviewed"] == 27 and t["buckets"]["high"]["n"] == 22 and t["buckets"]["low"]["edited_rate"] == 1.0
    assert t["monotonic"] is True  # 高把握度改得更少
    assert t["auto_send_candidates"] == ["shipping_status"]  # 22 条、被改 9% ≤ 10%
    assert t["per_intent"]["fitment"]["low"]["n"] == 5
