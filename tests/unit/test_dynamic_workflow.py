"""Testes da Fase 3: Motor de Workflow Dinâmico e DAG de Etapas.

Valida:
1. Validação e ordenação topológica do WorkflowDAG.
2. Detecção de ciclos e dependências inválidas.
3. Execução condicionada e dinâmica pelo WorkflowEngine.
4. Salto de revisão cruzada para tarefas simples/assets (skip_review / category='asset').
5. Integração do Coordinator com o WorkflowEngine no modo plan_only e completo.
"""
from __future__ import annotations

import asyncio
import pytest

from agents.mock_adapters import build_mock_backends, plan_json, symmetric_responder
from core.workflow import (
    CycleDetectedError,
    InvalidStageDependencyError,
    WorkflowContext,
    WorkflowDAG,
    WorkflowEngine,
    WorkflowStage,
    WorkflowStageStatus,
)
from tests.conftest import run


def test_workflow_dag_topological_sort_and_waves():
    s1 = WorkflowStage(id="stage_1", name="S1", action_name="s1", depends_on=())
    s2 = WorkflowStage(id="stage_2", name="S2", action_name="s2", depends_on=("stage_1",))
    s3 = WorkflowStage(id="stage_3", name="S3", action_name="s3", depends_on=("stage_1",))
    s4 = WorkflowStage(id="stage_4", name="S4", action_name="s4", depends_on=("stage_2", "stage_3"))

    dag = WorkflowDAG([s1, s2, s3, s4])
    order = [s.id for s in dag.topological_sort()]
    assert order[0] == "stage_1"
    assert order[-1] == "stage_4"
    assert set(order[1:3]) == {"stage_2", "stage_3"}

    waves = [[s.id for s in wave] for wave in dag.execution_waves()]
    assert waves[0] == ["stage_1"]
    assert set(waves[1]) == {"stage_2", "stage_3"}
    assert waves[2] == ["stage_4"]


def test_workflow_dag_cycle_detection():
    s1 = WorkflowStage(id="a", name="A", action_name="a", depends_on=("c",))
    s2 = WorkflowStage(id="b", name="B", action_name="b", depends_on=("a",))
    s3 = WorkflowStage(id="c", name="C", action_name="c", depends_on=("b",))

    dag = WorkflowDAG([s1, s2, s3])
    with pytest.raises(CycleDetectedError):
        dag.topological_sort()


def test_workflow_dag_invalid_dependency():
    s1 = WorkflowStage(id="a", name="A", action_name="a", depends_on=("non_existent",))
    dag = WorkflowDAG([s1])
    with pytest.raises(InvalidStageDependencyError):
        dag.validate()


@pytest.mark.anyio
async def test_workflow_engine_execution_and_condition_skipping():
    dag = WorkflowDAG([
        WorkflowStage(id="init", name="Init", action_name="init", depends_on=()),
        WorkflowStage(id="optional", name="Optional", action_name="optional", depends_on=("init",),
                      condition=lambda ctx: ctx.extra.get("run_optional", False)),
        WorkflowStage(id="conclude", name="Conclude", action_name="conclude", depends_on=("init",)),
    ])

    executed = []

    async def h_init(ctx: WorkflowContext):
        executed.append("init")
        return "init_ok"

    async def h_optional(ctx: WorkflowContext):
        executed.append("optional")
        return "optional_ok"

    async def h_conclude(ctx: WorkflowContext):
        executed.append("conclude")
        return "concluded"

    engine = WorkflowEngine()
    ctx = WorkflowContext(wf_id="wf-1", goal="Test dynamic engine", extra={"run_optional": False})

    res = await engine.execute(
        dag,
        ctx,
        {"init": h_init, "optional": h_optional, "conclude": h_conclude},
    )

    assert res == "concluded"
    assert executed == ["init", "conclude"]
    assert ctx.stage_statuses["init"] == WorkflowStageStatus.COMPLETED
    assert ctx.stage_statuses["optional"] == WorkflowStageStatus.SKIPPED
    assert ctx.stage_statuses["conclude"] == WorkflowStageStatus.COMPLETED


def test_subtask_skips_review_for_asset_category(harness, workspace):
    """Subtarefa com categoria 'asset' ou 'skip_review: true' pula a revisão de LLM."""
    async def go():
        task_asset = {
            "id": "T1",
            "assigned_to": "deepseek",
            "category": "asset",
            "title": "Gerar asset de textura",
            "objective": "Criar textura",
            "instructions": "Criar src/shared/Texture.lua",
            "files": ["src/shared/Texture.lua"],
            "acceptance_criteria": ["Textura gerada"],
            "constraints": [],
            "depends_on": [],
        }
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", tasks=[task_asset])
        b["deepseek"].responder = symmetric_responder("deepseek", files=["src/shared/Texture.lua"])

        async with harness(b) as h:
            r = await h.client.post("/tasks", json={"goal": "Criar textura para jogo"})
            wf_id = r.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == "completed"
            st = d["subtasks"][0]
            assert st["status"] == "approved"
            assert st["review_cycles"] == 0

            # Verifica que a revisão foi dispensada pela política de assets
            events = [e["event"] for e in d["events"]]
            assert "review_skipped_by_policy" in events
            # Confirma que o Gemini não foi chamado para review_implementation desta subtarefa
            gemini_skills = [c["skill"] for c in b["gemini"].calls]
            assert "review_implementation" not in gemini_skills

    run(go())


def test_subtask_explicit_skip_review_flag(harness, workspace):
    """Subtarefa com 'skip_review: true' explícito dispensa revisão de LLM."""
    async def go():
        task_skip = {
            "id": "T1",
            "assigned_to": "deepseek",
            "category": "boilerplate",
            "skip_review": True,
            "title": "Gerar boilerplate sem revisão",
            "objective": "Criar código",
            "instructions": "Criar src/shared/Boilerplate.lua",
            "files": ["src/shared/Boilerplate.lua"],
            "acceptance_criteria": [],
            "constraints": [],
            "depends_on": [],
        }
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", tasks=[task_skip])
        b["deepseek"].responder = symmetric_responder("deepseek", files=["src/shared/Boilerplate.lua"])

        async with harness(b) as h:
            r = await h.client.post("/tasks", json={"goal": "Criar código simples"})
            wf_id = r.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == "completed"
            assert "review_skipped_by_policy" in [e["event"] for e in d["events"]]

    run(go())


def test_plan_only_mode_runs_via_dynamic_dag(harness):
    """Workflow com mode='plan_only' usa o plan_only_dag do motor dinâmico."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            r = await h.client.post("/tasks", json={"goal": "Apenas planejar sistema", "mode": "plan_only"})
            wf_id = r.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] == "planned"
            assert d["result"]["plan"] is not None
            # Verifica que etapas posteriores não foram executadas
            assert "assignment_proposed" not in [e["event"] for e in d["events"]]
            assert "implementation_report" not in [e["event"] for e in d["events"]]

    run(go())

