"""黄金集补充：来源 / 标签来源分布、复核表导出导入（管理员 / 售后，导入记审计）、当时模型输出带版本、报告按来源分组。"""
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from conftest import login, make_user


@pytest.fixture()
def golden_file(app_env, tmp_path, monkeypatch):
    from app.evaluation import golden

    p = tmp_path / "golden.jsonl"
    monkeypatch.setattr(golden, "GOLDEN_FILE", p)
    golden.dump(golden.load(golden.SAMPLE_FILE)[:3] + [{**golden.load(golden.SAMPLE_FILE)[3], "case_id": "G-auto1",
                                                        "label_source": "auto", "needs_review": True, "source": "manual"}], p)
    return p


def test_distribution():
    from app.evaluation import golden, review

    d = review.distribution(golden.load(golden.SAMPLE_FILE))
    assert d["source"] == {"synthetic": 10} and d["label_source"] == {"human": 10} and d["needs_review"] == 0


def test_review_roundtrip_roles_and_audit(golden_file, client):
    from sqlalchemy import select

    from app.db import session_scope
    from app.evaluation import golden
    from app.models import AuditEvent

    r = client.get("/internal/eval/golden/review.csv")
    assert r.status_code == 200 and r.text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    row = next(x for x in rows if x["case_id"] == "G-auto1")
    assert row["label_source"] == "auto" and row["reviewed"] == ""
    row.update(intent="item_spec", action="answer", handoff="false", compliance="PASS,REVIEW", reviewed="1")
    bad = {**rows[0], "intent": "no_such_intent", "reviewed": "1"}
    buf = io.StringIO()
    w = csv.DictWriter(buf, list(rows[0]))
    w.writeheader()
    w.writerows([row, bad])
    res = client.post("/internal/eval/golden/review", json={"csv": buf.getvalue()}).json()
    assert res["imported"] == 1 and len(res["errors"]) == 1 and "no_such_intent" in res["errors"][0]
    case = next(c for c in golden.load(golden_file) if c["case_id"] == "G-auto1")
    assert case["label_source"] == "human" and not case["needs_review"] and case["expect"]["compliance"] == ["PASS", "REVIEW"]
    assert res["distribution"]["label_source"] == {"human": 4}
    with session_scope() as db:
        a = db.scalars(select(AuditEvent).where(AuditEvent.action == "EVAL_LABELS_IMPORTED")).one()
        assert a.actor == "tester" and a.detail["case_ids"] == ["G-auto1"] and a.detail["count"] == 1
    assert client.get("/internal/eval/golden/summary").json()["cases"] == 4
    make_user("cs_rev", "CS")
    cs = TestClient(client.app)
    login(cs, "cs_rev")
    assert cs.get("/internal/eval/golden/review.csv").status_code == 403
    assert cs.post("/internal/eval/golden/review", json={"csv": buf.getvalue()}).status_code == 403
    make_user("as_rev", "AFTERSALES")
    af = TestClient(client.app)
    login(af, "as_rev")
    assert af.get("/internal/eval/golden/review.csv").status_code == 200


def test_model_meta_recorded(app_env):
    from app.db import session_scope
    from app.evaluation.builder import _model_meta
    from app.models import AgentRun, AgentStep, Conversation, Draft, ReplyJob, Store

    with session_scope() as db:
        db.add(Store(id="MM", name="m", timezone="UTC", default_marketplace_id="EBAY_GB"))
        db.flush()
        conv = Conversation(store_id="MM", ebay_conversation_id="c", buyer_key="b")
        db.add(conv)
        db.flush()
        job = ReplyJob(store_id="MM", conversation_id=conv.id, anchor_source_message_id="m", state="SENT")
        db.add(job)
        db.flush()
        run = AgentRun(job_id=job.id, run_generation=1, thread_id="t", mode="fixed_pipeline", rule_version="rules-x")
        db.add(run)
        db.flush()
        db.add(AgentStep(run_id=run.id, node="compose_reply", result_status="OK", detail={"model": "qwen-plus"}))
        d = Draft(job_id=job.id, run_generation=1, version=1, author="agent", conversation_row_version=1, text="hi",
                  prompt_version="pv-abc", model_config_version="mc-1", knowledge_versions=["kb:X@3"],
                  compliance_rules_version="compliance-1")
        db.add(d)
        db.flush()
        meta = _model_meta(db, job.id, d)
    assert meta["prompt_version"] == "pv-abc" and meta["model"] == "qwen-plus" and meta["model_config_version"] == "mc-1"
    assert meta["rule_version"] == "rules-x" and meta["knowledge_versions"] == ["kb:X@3"]
    assert meta["compliance_rules_version"] == "compliance-1"


def test_report_groups_by_source_label_and_prompt():
    from app.evaluation.rules_eval import aggregate

    rows = [{"case_id": "a", "ok": True, "mode": "recorded", "source": "agent", "label_source": "human", "prompt_version": "pv-1",
             "stratum": "x", "checks": {}},
            {"case_id": "b", "ok": False, "mode": "recorded", "source": "agent", "label_source": "auto", "prompt_version": "pv-2",
             "stratum": "x", "checks": {}},
            {"case_id": "c", "ok": None, "mode": "skip", "source": "synthetic", "label_source": "synthetic", "stratum": "x"}]
    s = aggregate(rows)
    assert s["by_source"] == {"agent": {"n": 2, "pass_rate": 0.5}}
    assert s["by_label_source"]["human"]["pass_rate"] == 1.0 and s["by_prompt_version"]["pv-2"]["pass_rate"] == 0.0
    assert s["dataset"] == {"source": {"agent": 2, "synthetic": 1}, "label_source": {"human": 1, "auto": 1, "synthetic": 1}}
