"""Testes das melhorias da Fase 2:
1. Cancelamento Abrangente em qualquer status não-terminal (planned, needs_human, paused, etc.).
2. Liberação imediata de locks e cancelamento de processos no cancelamento.
3. Modo Simples / Econômico (Fast-Track: pula negociação LLM e dispensa revisão final para 1 subtarefa).
"""
import pytest
from agents.mock_adapters import build_mock_backends
from orchestrator.task_manager import ST, WF
from tests.conftest import run
from tests.unit.test_symmetric_flow import start, writer, task_for


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

            # Confirma que revisão final foi dispensada e aprovada automaticamente
            assert "final_review_passed" in events
            assert b["gemini"].skill_calls("final_review") == []
            assert b["deepseek"].skill_calls("final_review") == []
            assert d["final_review_state"] == "passed"

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
