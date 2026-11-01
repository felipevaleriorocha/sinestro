"""[MOCK backends + A2A REAL] Servidores e cliente do SDK oficial a2a-sdk conversando via JSON-RPC (ASGI)."""
import asyncio
import sqlite3

from a2a.client import A2ACardResolver
from a2a.types import AgentCard

from agents.mock_adapters import build_mock_backends
from communication.a2a_client import A2ADelegationError
from tests.conftest import run


def test_agent_cards_have_identity_and_skills(harness):
    async def go():
        async with harness(build_mock_backends()) as h:
            symmetric = {"plan_feature", "respond_assignment", "implement_task", "review_implementation", "final_review"}
            for name, skills in (("gemini", symmetric), ("deepseek", symmetric)):
                r = await h.client.get(f"/a2a/{name}/.well-known/agent-card.json")
                assert r.status_code == 200
                card = r.json()
                assert {s["id"] for s in card["skills"]} == skills
                iface = card["supportedInterfaces"][0]
                assert iface["protocolBinding"] == "JSONRPC" and iface["protocolVersion"] == "1.0"
                assert iface["url"].endswith(f"/a2a/{name}/")
            card = await A2ACardResolver(h.client, "http://127.0.0.1:8787/a2a/gemini").get_agent_card()
            assert isinstance(card, AgentCard) and "Tech Lead" in card.name
            disc = (await h.client.get("/a2a/discovery")).json()
            assert disc["gemini"]["reachable"] and disc["deepseek"]["reachable"]
    run(go())


def test_send_message_task_lifecycle_artifacts_and_history(harness):
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            a2a = h.app.state.container.a2a
            task_id, ctx = await a2a.send("deepseek", "implement_task",
                                          {"goal": "g", "task": {"title": "t", "files": []}, "context_text": "CTX"},
                                          summary="teste", workflow_id="wf-x", subtask_id="st-x", sender="gemini")
            assert task_id and ctx
            res = await a2a.wait("deepseek", task_id, skill="implement_task", workflow_id="wf-x", requester="gemini")
            assert res.state == "completed" and res.data["status"] == "done"
            assert res.artifacts and res.artifacts[0]["name"] == "implement_task_result"
            t2, ctx2 = await a2a.send("deepseek", "implement_task", {"goal": "g", "task": {"title": "t"}, "feedback": {"verdict": "changes_requested"}},
                                      summary="correção", context_id=ctx, reference_task_ids=[task_id], workflow_id="wf-x")
            assert ctx2 == ctx
            await a2a.wait("deepseek", t2, skill="implement_task", workflow_id="wf-x")
            assert b["deepseek"].calls[0]["context_key"] == b["deepseek"].calls[1]["context_key"] == ctx
            msgs = h.app.state.container.repo.list_messages("wf-x")
            assert [m["direction"] for m in msgs] == ["outbound", "inbound", "outbound", "inbound"]
            assert msgs[0]["sender"] == "gemini" and msgs[0]["recipient"] == "deepseek"
            assert msgs[0]["payload"]["context_chars"] == 3
            runs = h.app.state.container.repo.list_runs("deepseek")
            assert runs[0]["status"] == "completed" and runs[0]["usage"]
        db = h.app.state.container.settings.db_path.with_name("orchestrator_a2a.db")
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 2
    run(go())


def test_unsupported_skill_rejected_by_card_check(harness):
    async def go():
        async with harness(build_mock_backends()) as h:
            try:
                await h.app.state.container.a2a.send("deepseek", "deploy_to_production", {}, summary="x")
                raise AssertionError("deveria falhar")
            except A2ADelegationError as e:
                assert e.code == "unsupported_skill"
    run(go())


def test_quota_rejects_task_without_running_model(harness):
    async def go():
        b = build_mock_backends()
        b["deepseek"].quota_reason = "Quota excedida (simulada)"
        async with harness(b) as h:
            res = await h.app.state.container.a2a.delegate("deepseek", "implement_task", {"goal": "g", "task": {"title": "t"}}, summary="x")
            assert res.state == "rejected" and res.error_code == "quota_exceeded"
            assert b["deepseek"].calls == []
    run(go())


def test_cancel_running_task(harness):
    async def go():
        b = build_mock_backends(delay=5.0)
        async with harness(b) as h:
            a2a = h.app.state.container.a2a
            tid, _ = await a2a.send("deepseek", "implement_task", {"goal": "g", "task": {"title": "t"}}, summary="x")
            await asyncio.sleep(0.3)
            assert await a2a.cancel("deepseek", tid) == "canceled"
            res = await a2a.wait("deepseek", tid, skill="implement_task")
            assert res.state == "canceled"
    run(go())


def test_backend_error_becomes_failed_state_with_code(harness):
    async def go():
        from agents.base import AgentTimeoutError
        b = build_mock_backends()
        b["deepseek"].raise_on_run = AgentTimeoutError("demorou demais")
        async with harness(b) as h:
            res = await h.app.state.container.a2a.delegate("deepseek", "implement_task", {"goal": "g", "task": {"title": "t"}}, summary="x")
            assert res.state == "failed" and res.error_code == "timeout"
    run(go())
