import json
import pytest
from pathlib import Path
from orchestrator.file_locks import FileLockManager
from database.repository import Repository
from communication.a2a_client import A2ADelegationError
from agents.mock_adapters import build_mock_backends, plan_json, symmetric_responder
from tests.conftest import run
from orchestrator.task_manager import WF


def test_expired_locks_require_reconciliation(make_settings, workspace):
    s = make_settings()
    repo = Repository(s.db_path)
    locks = FileLockManager(repo, workspace, ttl_seconds=0)
    res = locks.file_resource("src/shared/A.lua")
    locks.acquire([res], "deepseek", "wf1", "st1", ttl_seconds=0)

    locks.release_expired()
    active = locks.list_locks()
    assert len(active) == 1
    assert active[0]["resource"] == res

    history = locks.history()
    assert any(h["action"] == "expired_retained" for h in history)

    locks.release_expired(force=True)
    assert len(locks.list_locks()) == 0


def test_cancel_unconfirmed_retains_locks(harness, workspace):
    async def go():
        b = build_mock_backends()

        def raise_timeout(*args, **kw):
            raise A2ADelegationError("timeout ao contatar agente", "timeout")

        async with harness(b) as h:
            h.app.state.container.a2a.delegate = raise_timeout
            res = await h.client.post("/tasks", json={"goal": "Forcar timeout lock"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id, timeout=10)
            assert d["status"] == "failed"
            events = [e["event"] for e in d["events"]]
            assert "locks_retained_unconfirmed_stop" in events

    run(go())


def test_subtask_stage_log_matches_db_status(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", ["changes_requested"] * 10)
        async with harness(b, MAX_REVIEW_CYCLES=0) as h:
            res = await h.client.post("/tasks", json={"goal": "Esgotar ciclos"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == "needs_human"
            stages = (await h.client.get(f"/tasks/{wf_id}/report")).json()["stages"]
            sub_stage = next((s for s in stages if s["stage"] == "subtask"), None)
            assert sub_stage is not None
            assert sub_stage["status"] == "needs_human"

    run(go())


def test_invalid_plan_reformulation_needs_human(harness, workspace):
    async def go():
        b = build_mock_backends()

        def bad_plan(skill, prompt, calls):
            return plan_json([{"id": "T1", "title": "A"}, {"id": "T1", "title": "B"}])

        b["gemini"].responder = bad_plan
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Plano duplicado"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == "needs_human"
            assert d["plan_reformulations"] == 1
            assert any(e["event"] == "plan_invalid_needs_human" for e in d["events"])

    run(go())


def test_dependency_analyzer_consumes_policy(harness, tmp_path):
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            policy_path = h.app.state.container.settings.dependency_policy_file
            orig_text = policy_path.read_text(encoding="utf-8") if policy_path.is_file() else None
            try:
                policy_path.parent.mkdir(parents=True, exist_ok=True)
                policy_path.write_text(json.dumps({"version": "9.9", "mcp_parallel_safe": True}), encoding="utf-8")
                res = await h.client.post("/tasks", json={"goal": "Ler policy"})
                wf_id = res.json()["id"]
                d = await h.wait_done(wf_id)
                assert d["dependency_analysis"]["policy_version"] == "9.9"
                res = await h.client.post(f"/tasks/{wf_id}/analyze")
                assert res.json()["policy_version"] == "9.9"
            finally:
                if orig_text is not None:
                    policy_path.write_text(orig_text, encoding="utf-8")
                elif policy_path.exists():
                    policy_path.unlink()

    run(go())
