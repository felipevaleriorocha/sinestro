"""[MOCK] Reservas de arquivos, conflitos, expiração, histórico, contexto compartilhado e MCP."""
import json
import time

import pytest

from agents.mock_adapters import build_mock_backends
from database.repository import Repository
from mcp_bridge.server_registry import MCPServerRegistry
from memory.context_manager import ContextManager
from memory.decision_store import DecisionStore
from orchestrator.file_locks import FileLockManager, LockConflictError, PathOutsideWorkspaceError
from tests.conftest import run


@pytest.fixture
def locks(make_settings, workspace):
    s = make_settings()
    return FileLockManager(Repository(s.db_path), workspace, ttl_seconds=60)


def test_conflict_between_agents_on_same_file(locks):
    res = locks.file_resource("src/shared/A.lua")
    locks.acquire([res], "deepseek", "wf", "st1")
    with pytest.raises(LockConflictError) as e:
        locks.acquire([locks.file_resource("./src\\shared/A.lua")], "gemini", "wf", "st2")
    assert e.value.conflicts[0]["owner"] == "deepseek"
    locks.release([res], "deepseek", "st1")
    locks.acquire([res], "gemini", "wf", "st2")
    assert {"acquired", "conflict", "released"} <= {h["action"] for h in locks.history()}


def test_acquire_is_all_or_nothing(locks):
    a, b = locks.file_resource("a.lua"), locks.file_resource("b.lua")
    locks.acquire([b], "gemini", "wf", "s1")
    with pytest.raises(LockConflictError):
        locks.acquire([a, b], "deepseek", "wf", "s2")
    assert [l["resource"] for l in locks.list_locks()] == [b]


def test_expired_reservations_are_released(locks):
    res = locks.file_resource("x.lua")
    locks.acquire([res], "deepseek", "wf", "s1", ttl_seconds=1)
    time.sleep(1.2)
    locks.acquire([res], "gemini", "wf", "s2")
    assert any(h["action"] == "expired" for h in locks.history())


def test_paths_outside_workspace_rejected(locks):
    for bad in ("../fora.lua", "/etc/passwd", "src/../../fora.lua"):
        with pytest.raises(PathOutsideWorkspaceError):
            locks.file_resource(bad)


def test_mcp_resources_use_same_lock_manager(locks):
    r = locks.mcp_resource("roblox_studio", "Workspace/Map/Spawn")
    locks.acquire([r], "gemini", "wf", "map-task")
    with pytest.raises(LockConflictError):
        locks.acquire([r], "deepseek", "wf", "other")


def test_change_outside_reservation_is_flagged(harness, workspace):
    async def go():
        b = build_mock_backends()

        def respond(skill, prompt, calls):
            if skill != "implement_task":
                return json.dumps({"responses": []})
            (workspace / "src" / "shared" / "Example.lua").write_text("return {}\n")
            (workspace / "README.md").write_text("alterado fora da reserva\n")
            return json.dumps({"status": "done", "summary": "ok", "files_modified": ["src/shared/Example.lua", "README.md"]})

        b["deepseek"].responder = respond
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Conflito de arquivo"})).json()["id"])
            changes = {c["path"]: c["within_reservation"] for c in (await h.client.get(f"/files/changes?workflow_id={d['id']}")).json()}
            assert changes["README.md"] == 0 and changes["src/shared/Example.lua"] == 1
            assert any(e["event"] == "file_conflict" for e in d["events"])
    run(go())


def test_parallel_only_for_independent_subtasks(harness, workspace):
    async def go():
        b = build_mock_backends()

        def plan(skill, prompt, calls):
            if skill == "plan_feature":
                return json.dumps({"summary": "p", "tasks": [
                    {"id": "T1", "agent": "deepseek", "title": "A", "files": ["a.lua"]},
                    {"id": "T2", "agent": "deepseek", "title": "B", "files": ["a.lua"]},
                    {"id": "T3", "agent": "deepseek", "title": "C", "files": ["c.lua"], "depends_on": ["T1"]}]})
            return json.dumps({"verdict": "approved", "summary": "ok"})

        b["gemini"].responder = plan
        async with harness(b, MAX_PARALLEL_SUBTASKS=3, FINAL_REVIEW="false") as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Paralelismo"})).json()["id"])
            assert d["status"] == "completed"
            history = (await h.client.get("/locks/history?limit=100")).json()
            assert not any(x["action"] == "conflict" for x in history)
    run(go())


def test_shared_context_same_for_both_agents_and_reports_missing_docs(make_settings, workspace, harness):
    s = make_settings()
    ds = DecisionStore(Repository(s.db_path))
    ds.add("Padrão de módulos", "ModuleScripts em src/shared", "user")
    bundle = ContextManager(workspace, s.context_sources_file, ds).build()
    assert {"agents", "readme", "gdd"} <= {x["category"] for x in bundle.sources}
    assert "Planejamento Mundos 1 e 2" in bundle.missing and "Documentação de arquitetura" in bundle.missing
    assert "não invente" in bundle.text and "ModuleScripts em src/shared" in bundle.text

    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            await h.wait_done((await h.client.post("/tasks", json={"goal": "Contexto igual"})).json()["id"])
            ctx_of = lambda p: p.split("CONTEXTO COMPARTILHADO:", 1)[1].strip()  # noqa: E731
            plan_ctx = ctx_of(b["gemini"].calls[0]["prompt"])
            dev = next(c for c in b["deepseek"].calls if c["skill"] == "implement_task")
            dev_ctx = ctx_of(dev["prompt"])
            assert "--- [AGENTS.md] AGENTS.md ---" in plan_ctx
            assert plan_ctx.split("--- DECISÕES")[0] == dev_ctx.split("--- DECISÕES")[0]
            assert "Módulos em src/shared" in dev_ctx
    run(go())


def test_mcp_registry_states(tmp_path):
    cfg = tmp_path / "mcp.json"
    reg = MCPServerRegistry(cfg)
    assert reg.status() == {}
    cfg.write_text(json.dumps({"servers": {"roblox_studio": {"enabled": True, "health_check": {"type": "none"},
                                                              "agents": {"gemini": {"enabled": True, "role": "map"},
                                                                         "deepseek": {"enabled": False, "role": "scripts"}}}}}))
    st = reg.status()["roblox_studio"]
    assert st["agents"]["gemini"]["state"] == "unknown" and st["agents"]["deepseek"]["state"] == "not_configured"
    assert reg.agent_has_access("roblox_studio", "gemini") is False
