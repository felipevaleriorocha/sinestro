"""[MOCK backends + A2A REAL] Fluxo completo: planejamento, delegação, execução, revisão, correção, conclusão."""
import asyncio
import json
from pathlib import Path

from agents.mock_adapters import build_mock_backends, default_deepseek_responder
from tests.conftest import run


def writing_deepseek(workspace: Path, rel: str = "src/shared/Example.lua", report_files=None):
    """DeepSeek simulado que realmente escreve no workspace de TESTE (temporário)."""
    base = default_deepseek_responder(report_files if report_files is not None else [rel])

    def respond(skill, prompt, calls):
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"return {{ version = {calls['count']} }}\n", encoding="utf-8")
        return base(skill, prompt, calls)

    return respond


def test_create_task_plan_delegate_review_approve(harness, workspace):
    async def go():
        b = build_mock_backends(["approved"])
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b) as h:
            r = await h.client.post("/tasks", json={"goal": "Criar módulo de exemplo"})
            assert r.status_code == 202
            d = await h.wait_done(r.json()["id"])
            assert d["status"] == "completed", d["error"]
            st = d["subtasks"][0]
            assert st["status"] == "approved" and st["review_cycles"] == 1 and st["agent"] == "deepseek"
            assert st["result"]["files_modified"] == ["src/shared/Example.lua"]
            assert [c["skill"] for c in b["gemini"].calls] == ["plan_feature", "review_implementation", "final_review"]
            p = b["deepseek"].calls[0]["prompt"]
            for marker in ("OBJETIVO DA SUBTAREFA", "CONTEXTO COMPARTILHADO", "ARQUIVOS RESERVADOS", "CRITÉRIOS DE ACEITAÇÃO", "RESTRIÇÕES TÉCNICAS"):
                assert marker in p
            changes = (await h.client.get(f"/files/changes?workflow_id={d['id']}")).json()
            assert changes and changes[0]["path"] == "src/shared/Example.lua" and changes[0]["within_reservation"] == 1
            assert (await h.client.get("/locks")).json() == []
    run(go())


def test_review_requests_changes_then_approves(harness, workspace):
    async def go():
        b = build_mock_backends(["changes_requested", "approved"])
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b) as h:
            wf = (await h.client.post("/tasks", json={"goal": "Criar módulo com revisão"})).json()["id"]
            d = await h.wait_done(wf)
            assert d["status"] == "completed"
            assert d["subtasks"][0]["review_cycles"] == 2
            impl = [c for c in b["deepseek"].calls if c["skill"] == "implement_task"]
            assert len(impl) == 2
            assert "REVISÃO DE Gemini" in impl[1]["prompt"] and "ajuste simulado" in impl[1]["prompt"]
            events = [e["event"] for e in d["events"]]
            assert "correction_requested" in events and "review_approved" in events
            msgs = (await h.client.get(f"/tasks/{wf}/messages")).json()
            out = [m for m in msgs if m["skill"] == "implement_task" and m["direction"] == "outbound"]
            assert len(out) == 2 and out[0]["a2a_context_id"] == out[1]["a2a_context_id"]
    run(go())


def test_review_cycle_limit_prevents_infinite_loop(harness, workspace):
    async def go():
        b = build_mock_backends(["changes_requested"] * 10)
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b, MAX_REVIEW_CYCLES=1) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Loop de revisão"})).json()["id"])
            st = d["subtasks"][0]
            assert st["status"] == "needs_attention" and "Limite" in st["error"]
            assert len([c for c in b["deepseek"].calls if c["skill"] == "implement_task"]) == 2
            assert d["status"] == "failed"
    run(go())


def test_reviewer_rejects_with_justification(harness, workspace):
    async def go():
        b = build_mock_backends(["rejected"])
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Rejeição"})).json()["id"])
            st = d["subtasks"][0]
            assert st["status"] == "rejected" and "inadequada" in st["error"]
            decisions = (await h.client.get("/decisions?status=rejected")).json()
            assert any(x["author"] == "deepseek" for x in decisions)
    run(go())


def test_clarification_questions_are_sent_back(harness, workspace):
    async def go():
        b = build_mock_backends(["needs_clarification", "approved"])
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Esclarecimento"})).json()["id"])
            assert d["status"] == "completed"
            impl = [c for c in b["deepseek"].calls if c["skill"] == "implement_task"]
            assert "por quê?" in impl[1]["prompt"]
    run(go())


def test_deepseek_quota_blocks_workflow_without_calling_model(harness):
    async def go():
        b = build_mock_backends()
        b["deepseek"].quota_reason = "Quota do modelo excedida (simulada)"
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Sem cota"})).json()["id"])
            assert d["status"] == "blocked"
            assert d["subtasks"][0]["status"] == "blocked" and "Quota" in d["subtasks"][0]["error"]
            assert b["deepseek"].calls == []
            agents = {a["name"]: a for a in (await h.client.get("/agents")).json()}
            assert agents["deepseek"]["accepting_work"] is False
            b["deepseek"].quota_reason = None
            r = await h.client.post(f"/tasks/{d['id']}/resume")
            assert r.status_code == 202
            d2 = await h.wait_done(d["id"])
            assert d2["status"] == "completed"
            assert [c["skill"] for c in b["gemini"].calls].count("plan_feature") == 1
    run(go())


def test_plan_only_mode(harness):
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Só planejar", "mode": "plan_only"})).json()["id"])
            assert d["status"] == "planned" and d["plan"]["tasks"][0]["agent"] == "deepseek"
            assert b["deepseek"].calls == []
    run(go())


def test_gemini_failure_during_planning(harness):
    async def go():
        from agents.base import AgentResponseError
        b = build_mock_backends()
        b["gemini"].raise_on_run = AgentResponseError("Antigravity terminou com status ERROR")
        async with harness(b) as h:
            d = await h.wait_done((await h.client.post("/tasks", json={"goal": "Falha no plano"})).json()["id"])
            assert d["status"] == "failed" and "Planejamento falhou" in d["error"]
            errs = (await h.client.get("/errors")).json()
            assert errs["runs"] and errs["runs"][0]["status"] == "failed"
    run(go())


def test_cancel_workflow(harness, workspace):
    async def go():
        b = build_mock_backends(delay=3.0)
        async with harness(b) as h:
            wf = (await h.client.post("/tasks", json={"goal": "Cancelar no meio"})).json()["id"]
            await asyncio.sleep(0.3)
            assert (await h.client.post(f"/tasks/{wf}/cancel")).status_code == 200
            d = await h.wait_done(wf, timeout=30)
            assert d["status"] in {"canceled", "failed"}
            assert d["cancel_requested"] is True
    run(go())


def test_history_persists_after_restart(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = writing_deepseek(workspace)
        async with harness(b) as h:
            wf = (await h.client.post("/tasks", json={"goal": "Persistência"})).json()["id"]
            await h.wait_done(wf)
            db_path = h.app.state.container.settings.db_path
            h.app.state.container.repo.create_workflow("stale-1", "rodando quando caiu", "full", "0.3.0")
            h.app.state.container.repo.update_workflow("stale-1", status="delegating")
        async with harness(build_mock_backends()) as h2:
            assert h2.app.state.container.settings.db_path == db_path
            d = (await h2.client.get(f"/tasks/{wf}")).json()
            assert d["status"] == "completed" and d["subtasks"] and d["events"]
            assert len((await h2.client.get(f"/tasks/{wf}/messages")).json()) >= 6
            assert (await h2.client.get("/tasks/stale-1")).json()["status"] == "interrupted"
            assert (await h2.client.get("/decisions")).json()
    run(go())


def test_v02_database_is_migrated_without_data_loss(make_settings):
    import sqlite3
    s = make_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(s.db_path) as con:
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error TEXT)")
        con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, created_at TEXT NOT NULL, agent TEXT NOT NULL, event TEXT NOT NULL, content TEXT NOT NULL)")
        con.execute("INSERT INTO tasks VALUES('old','meta v0.2','completed','2026-01-01','2026-01-01',?,NULL)", (json.dumps({"x": 1}),))
        con.execute("INSERT INTO task_events(task_id, created_at, agent, event, content) VALUES('old','2026-01-01','gemini','plan','{}')")
    from database.repository import Repository
    repo = Repository(s.db_path)
    wf = repo.get_workflow("old")
    assert wf["goal"] == "meta v0.2" and wf["result"] == {"x": 1}
    assert repo.list_events("old")[0]["event"] == "plan"
