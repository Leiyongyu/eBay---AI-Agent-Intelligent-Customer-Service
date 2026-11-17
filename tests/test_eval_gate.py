"""版本门槛：版本变化检测、低于门槛记审计、基线、启动时检查（子进程不真起）、接口和状态页、CLI 在独立评测库里跑样例。"""
import pytest


def _summary(rate, cases=10):
    return {"cases": cases, "judged": cases, "pass_rate": rate, "by_check": {}, "by_stratum": {}, "failures": []}


def test_versions_change_detection(app_env):
    from app.db import session_scope
    from app.evaluation import gate
    from app.models import KnowledgeChunk

    with session_scope() as db:
        assert set(gate.changed(db)) == {"prompt", "rules", "compliance", "knowledge"}  # 从没评过
        gate.record(db, _summary(0.95))
        assert gate.changed(db) == []
        db.add(KnowledgeChunk(chunk_id="K-NEW", clause_id="CS-NEW-1", document_id="D", version="1", title="t", text="x",
                              store_id="ALL", marketplace_id="ALL", audience="PUBLIC_REPLY", language="en", topic="t",
                              priority=50, effective_at="2000-01-01", review_state="PUBLISHED", content_hash="h"))
        db.flush()
        assert gate.changed(db) == ["knowledge"]


def test_threshold_baseline_and_audit(app_env, monkeypatch):
    from sqlalchemy import select

    from app import config
    from app.db import session_scope
    from app.evaluation import gate
    from app.models import AuditEvent

    monkeypatch.setenv("EVAL_MIN_PASS_RATE", "0.9")
    monkeypatch.setenv("EVAL_MAX_DROP", "0.05")
    config.get_settings.cache_clear()
    with session_scope() as db:
        ok = gate.record(db, _summary(0.98), set_baseline=True)
        assert not ok.below and ok.is_baseline
        drop = gate.record(db, _summary(0.92))  # 过了门槛，但比基线 0.98 掉了 6 个点
        assert drop.below and drop.baseline_pass_rate == 0.98
        low = gate.record(db, _summary(0.8))
        assert low.below
        err = gate.record(db, {}, error="RuntimeError: boom")
        assert err.below
        audits = db.scalars(select(AuditEvent).where(AuditEvent.action == "EVAL_GATE_FAILED")).all()
        assert len(audits) == 3 and audits[0].detail["eval_run_id"] == drop.id
        with pytest.raises(ValueError):
            gate.mark_baseline(db, low.id, "tester")
        st = gate.status(db)
        assert st["red"] is True and st["baseline"]["id"] == ok.id and st["last"]["id"] == low.id  # 出错的那次不算“最近一次”
    config.get_settings.cache_clear()


def test_startup_check_spawns_only_when_changed(app_env, monkeypatch):
    from app import config
    from app.db import session_scope
    from app.evaluation import gate

    started = []
    monkeypatch.setattr(gate.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert gate.startup_check() is True and "--trigger" in started[0] and "startup" in started[0]
    with session_scope() as db:
        gate.record(db, _summary(0.95))
    assert gate.startup_check() is False and len(started) == 1  # 版本没变：不跑
    monkeypatch.setenv("EVAL_GATE_ON_STARTUP", "false")
    config.get_settings.cache_clear()
    with session_scope() as db:
        db.query(gate.EvalRun).delete()
    assert gate.startup_check() is False and len(started) == 1
    config.get_settings.cache_clear()


def test_eval_api_and_status(client):
    from app.db import session_scope
    from app.evaluation import gate

    with session_scope() as db:
        r = gate.record(db, _summary(0.97))
        rid = r.id
    st = client.get("/api/system/status").json()["eval_gate"]
    assert st["last"]["id"] == rid and st["red"] is False and st["threshold"] == 0.9
    assert client.get("/internal/eval/runs").json()[0]["id"] == rid
    assert client.post(f"/internal/eval/runs/{rid}/baseline").json()["is_baseline"] is True
    assert client.get("/internal/eval/gate").json()["baseline"]["id"] == rid


def test_cli_runs_sample_in_isolated_db(app_env, tmp_path):
    """CLI：在独立评测库里跑 10 条样例，--gate 通过返回 0；跑完连回原来的库。"""
    import importlib.util
    from pathlib import Path

    from app import config

    spec = importlib.util.spec_from_file_location("eval_rules_cli", Path(__file__).resolve().parents[1] / "scripts" / "eval_rules.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    before = config.get_settings().effective_database_url
    assert mod.main(["--sample", "--no-record", "--gate", "--quiet", "--out-dir", str(tmp_path / "run")]) == 0
    assert (tmp_path / "run" / "report.md").exists() and (tmp_path / "run" / "eval.db").exists()
    assert config.get_settings().effective_database_url == before
