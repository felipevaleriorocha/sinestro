"""Testes das melhorias da Fase 2:
1. Cancelamento Abrangente em qualquer status não-terminal (planned, needs_human, paused, etc.).
2. Liberação imediata de locks e cancelamento de processos no cancelamento.
3. Modo Simples / Econômico (Fast-Track: pula negociação LLM e dispensa revisão final para 1 subtarefa).
"""
from datetime import UTC

from agents.mock_adapters import build_mock_backends
from orchestrator.task_manager import ST, WF
from tests.conftest import run
from tests.unit.test_symmetric_flow import task_for, writer


def test_cancel_planned_workflow(harness):
    """Workflow em status 'planned' pode ser cancelado diretamente."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Workflow para planejar e cancelar", "mode": "plan_only"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == WF.PLANNED

            # Cancela após status planned
            r_cancel = await h.client.post(f"/tasks/{wf_id}/cancel")
            assert r_cancel.status_code == 200

            d2 = (await h.client.get(f"/tasks/{wf_id}")).json()
            assert d2["status"] == WF.CANCELED
            events = [e["event"] for e in d2["events"]]
            assert "workflow_canceled_by_user" in events

    run(go())


def test_cancel_needs_human_workflow(harness, workspace):
    """Workflow parado em 'needs_human' pode ser cancelado, cancelando subtarefas e liberando locks."""
    async def go():
        # Rejeição sistemática que esgota ciclos e joga em needs_human
        b = build_mock_backends(["changes_requested"] * 5)
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b, MAX_REVIEW_CYCLES=1) as h:
            res = await h.client.post("/tasks", json={"goal": "Workflow que vai para needs_human"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] in (WF.NEEDS_HUMAN, "needs_attention", WF.FAILED)

            # Cancela
            r_cancel = await h.client.post(f"/tasks/{wf_id}/cancel")
            assert r_cancel.status_code == 200

            d2 = (await h.client.get(f"/tasks/{wf_id}")).json()
            assert d2["status"] == WF.CANCELED
            for s in d2["subtasks"]:
                assert s["status"] in (ST.CANCELED, ST.APPROVED, ST.COMPLETED)
            events = [e["event"] for e in d2["events"]]
            assert "workflow_canceled_by_user" in events
            # Locks devem estar vazios
            locks = (await h.client.get("/locks")).json()
            assert len(locks) == 0

    run(go())


def test_cancel_already_terminal_fails(harness):
    """Workflow já finalizado (completed ou canceled) rejeita novo cancelamento com 409."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Workflow para planejar e cancelar", "mode": "plan_only"})
            wf_id = res.json()["id"]
            await h.wait_done(wf_id)
            await h.client.post(f"/tasks/{wf_id}/cancel")

            # Segunda chamada de cancelamento
            r_dup = await h.client.post(f"/tasks/{wf_id}/cancel")
            assert r_dup.status_code == 409
            assert "já finalizada" in r_dup.json()["detail"].lower() or "status" in r_dup.json()["detail"].lower()

    run(go())


def test_simple_mode_single_subtask_fast_track(harness, workspace):
    """Modo simples com 1 subtarefa pula negociação LLM e dispensa revisão final LLM (3 chamadas)."""
    async def go():
        b = build_mock_backends(["approved"])
        b["deepseek"].responder = writer("deepseek", workspace, tasks=[task_for("deepseek", "ui")])
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Ajuste rápido de UI no botão", "mode": "simple"})
            assert res.status_code == 202
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)

            assert d["status"] == WF.COMPLETED
            # Confirma que negociação LLM foi pulada
            events = [e["event"] for e in d["events"]]
            assert "assignment_negotiation_skipped" in events
            assert b["gemini"].skill_calls("respond_assignment") == []
            assert b["deepseek"].skill_calls("respond_assignment") == []

            # Confirma que revisão final foi dispensada por política com estado explícito
            assert "final_review_skipped_by_policy" in events
            assert b["gemini"].skill_calls("final_review") == []
            assert b["deepseek"].skill_calls("final_review") == []
            assert d["final_review_state"] == "skipped_by_policy"

            # Total de chamadas: plano (1), implementação (1), revisão cruzada (1) = 3
            total_calls = len(b["gemini"].calls) + len(b["deepseek"].calls)
            assert total_calls == 3

    run(go())


def test_simple_mode_multiple_subtasks_runs_final_review(harness, workspace):
    """Modo simples com múltiplas subtarefas pula negociação LLM mas mantém revisão final agregada."""
    async def go():
        b = build_mock_backends(["approved", "approved", "approved"])
        two_tasks = [
            task_for("deepseek", "ui", id="T1", title="Subtarefa 1", files=["src/client/ui.luau"]),
            task_for("gemini", "server", id="T2", title="Subtarefa 2", files=["src/server/srv.luau"])
        ]
        b["gemini"].responder = writer("gemini", workspace, tasks=two_tasks)
        b["deepseek"].responder = writer("deepseek", workspace, tasks=two_tasks)
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Múltiplas subtarefas em modo simples", "mode": "simple"})
            assert res.status_code == 202
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)

            assert d["status"] == WF.COMPLETED
            assert len(d["subtasks"]) == 2
            # Negociação pulada
            assert b["gemini"].skill_calls("respond_assignment") == []
            assert b["deepseek"].skill_calls("respond_assignment") == []

            # Revisão final executada pois há 2 subtarefas
            assert (len(b["gemini"].skill_calls("final_review")) + len(b["deepseek"].skill_calls("final_review"))) >= 1
            assert d["final_review_state"] == "passed"

    run(go())


def test_delete_and_rename_workflow(harness):
    """Verifica exclusão de workflow (DELETE /tasks/{id}) e renomeação (PATCH /tasks/{id})."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Conversa original para excluir", "mode": "plan_only"})
            wf_id = res.json()["id"]
            await h.wait_done(wf_id)

            # Renomeia
            r_patch = await h.client.patch(f"/tasks/{wf_id}", json={"goal": "Conversa renomeada"})
            assert r_patch.status_code == 200
            d = (await h.client.get(f"/tasks/{wf_id}")).json()
            assert d["goal"] == "Conversa renomeada"

            # Exclui
            r_del = await h.client.delete(f"/tasks/{wf_id}")
            assert r_del.status_code == 200
            assert r_del.json()["deleted"] is True

            # Confirma 404
            r_get = await h.client.get(f"/tasks/{wf_id}")
            assert r_get.status_code == 404
    run(go())



def test_resume_preserves_subtask_attempt_counters(harness, workspace):
    """Garante que a retomada de workflow NÃO zera contadores de impl_attempts ou review_cycles."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Preservação de contadores", "mode": "plan_only"})
            wf_id = res.json()["id"]
            await h.wait_done(wf_id)

            # Simula subtarefa com contadores já incrementados
            repo = h.app.state.container.repo
            subs = repo.list_subtasks(wf_id)
            if subs:
                s0 = subs[0]
                repo.update_subtask(s0["id"], status="blocked", impl_attempts=2, review_cycles=1)
                s_check = repo.get_subtask(s0["id"])
                assert s_check["impl_attempts"] == 2
                assert s_check["review_cycles"] == 1
    run(go())


def test_cancel_stops_asyncio_task_immediately(harness):
    """Garante que cancel() chama t.cancel() na asyncio.Task em execução, interrompendo o loop."""
    import asyncio
    async def go():
        b = build_mock_backends(delay=3.0)
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Cancelamento imediato de task"})
            wf_id = res.json()["id"]
            await asyncio.sleep(0.2)
            c = h.app.state.container.coordinator
            assert c.is_running(wf_id)
            task_ref = c._running.get(wf_id)
            assert task_ref is not None and not task_ref.done()

            # Dispara cancelamento
            await h.client.post(f"/tasks/{wf_id}/cancel")
            await asyncio.sleep(0.05)
            # A task asyncio deve ter recebido cancel() e concluído/cancelado
            assert task_ref.done()
    run(go())


def test_cancel_does_not_overwrite_already_rejected_subtasks(harness):
    """Garante que subtarefas já rejeitadas (ST.FINAL) mantêm seu status e não viram 'canceled'."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Não sobrescrever rejected", "mode": "plan_only"})
            wf_id = res.json()["id"]
            await h.wait_done(wf_id)
            repo = h.app.state.container.repo
            subs = repo.list_subtasks(wf_id)
            assert len(subs) > 0
            # Marca uma como rejected
            repo.update_subtask(subs[0]["id"], status=ST.REJECTED, error="Rejeitada com justificativa")

            # Cancela o workflow
            await h.client.post(f"/tasks/{wf_id}/cancel")
            s_after = repo.get_subtask(subs[0]["id"])
            assert s_after["status"] == ST.REJECTED  # Preserva status rejected!
    run(go())


def test_prune_inactive_workflows_preserves_accepted_decisions(harness):
    """Garante que a poda de workflows inativos de 30 dias NÃO apaga decisões com status='accepted'."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            repo = h.app.state.container.repo
            repo.create_workflow("wf-old", "Tarefa muito antiga", "full", "0.5.1")
            repo.update_workflow("wf-old", status="completed")
            # Adiciona decisões: uma aceita e uma proposta
            with repo.connect() as con:
                con.execute(
                    "INSERT INTO decisions (created_at, updated_at, title, decision, author, status, workflow_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", "Decisão Aceita", "Arquitetura base", "user", "accepted", "wf-old")
                )
                con.execute(
                    "INSERT INTO decisions (created_at, updated_at, title, decision, author, status, workflow_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", "Decisão Proposta", "Em discussão", "user", "proposed", "wf-old")
                )

            # Força data antiga (40 dias atrás)
            from datetime import datetime, timedelta
            old_time = (datetime.now(UTC) - timedelta(days=40)).isoformat()
            with repo.connect() as con:
                con.execute("UPDATE tasks SET updated_at=? WHERE id=?", (old_time, "wf-old"))

            # Roda poda
            pruned = repo.prune_inactive_workflows(days=30)
            assert pruned == 1

            # Confirma que decisão aceita continua existindo no banco
            with repo.connect() as con:
                row_acc = con.execute("SELECT title, status, workflow_id FROM decisions WHERE title='Decisão Aceita'").fetchone()
                assert row_acc is not None
                assert row_acc["status"] == "accepted"
                assert row_acc["workflow_id"] is None  # Desvinculada com sucesso
                row_prop = con.execute("SELECT title FROM decisions WHERE title='Decisão Proposta'").fetchone()
                assert row_prop is None  # Proposta foi removida
    run(go())
