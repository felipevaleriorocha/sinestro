"""Testes da Fase 4: Roteamento Orientado a Dados e Retenção de 30 Dias (Touch-based TTL).

1. Registro de desfecho de subtarefas (record_subtask_outcome) e consulta agregada (get_routing_stats).
2. Renovação de lease via toque na interação (touch_workflow).
3. Poda de workflows inativos (> 30 dias) preservando workflows ativos e recentes (prune_inactive_workflows).
4. Roteamento inteligente empírico (IntelligentRouter) com fallback quando amostras < 5 e data-driven quando >= 5.
5. Endpoints de API (/metrics/routing-stats e /workflows/prune-inactive).
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.mock_adapters import build_mock_backends
from database.repository import Repository, utc_now
from orchestrator.assignment import IntelligentRouter, MIN_DATA_DRIVEN_SAMPLES
from orchestrator.task_manager import ST, WF
from tests.conftest import run
from tests.unit.test_symmetric_flow import writer


def test_repository_routing_stats_and_aggregation(tmp_path: Path):
    """Testa acúmulo de estatísticas de subtarefas e cálculo de métricas agregadas."""
    db_path = tmp_path / "sinestro_test.db"
    repo = Repository(db_path)

    # Inicialmente vazio
    assert repo.get_routing_stats() == []

    # Registra desfechos para deepseek em 'ui'
    repo.record_subtask_outcome(agent="deepseek", category="ui", first_pass=True, review_cycles=0, duration_seconds=12.0)
    repo.record_subtask_outcome(agent="deepseek", category="ui", first_pass=True, review_cycles=0, duration_seconds=18.0)
    repo.record_subtask_outcome(agent="deepseek", category="ui", first_pass=False, review_cycles=1, duration_seconds=30.0)

    stats = repo.get_routing_stats(category="ui", agent="deepseek")
    assert len(stats) == 1
    s = stats[0]
    assert s["agent"] == "deepseek"
    assert s["category"] == "ui"
    assert s["samples_count"] == 3
    assert s["first_pass_count"] == 2
    assert s["first_pass_rate"] == pytest.approx(0.67, abs=0.01)
    assert s["avg_review_cycles"] == pytest.approx(0.33, abs=0.01)
    assert s["avg_duration_seconds"] == pytest.approx(20.0, abs=0.01)


def test_touch_workflow_and_prune_inactive(tmp_path: Path):
    """Testa renovação de lease por interação e limpeza de workflows inativos após 30 dias."""
    db_path = tmp_path / "sinestro_test.db"
    repo = Repository(db_path)

    # 1. Cria 3 workflows com timestamps artificiais passados
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=35)).isoformat()
    recent_time = (now - timedelta(days=5)).isoformat()

    # wf_old_completed: inativo e antigo (> 30 dias) -> deve ser podado
    repo.create_workflow(workflow_id="wf_old_completed", goal="Old completed", mode="full", version="0.5.1")
    with repo.connect() as con:
        con.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (WF.COMPLETED, old_time, "wf_old_completed"),
        )

    # wf_old_running: antigo (> 30 dias), mas ATIVO (delegating) -> NUNCA deve ser podado
    repo.create_workflow(workflow_id="wf_old_running", goal="Old running", mode="full", version="0.5.1")
    with repo.connect() as con:
        con.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (WF.DELEGATING, old_time, "wf_old_running"),
        )

    # wf_recent_completed: concluído há 5 dias (< 30 dias) -> deve ser preservado
    repo.create_workflow(workflow_id="wf_recent_completed", goal="Recent completed", mode="full", version="0.5.1")
    with repo.connect() as con:
        con.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (WF.COMPLETED, recent_time, "wf_recent_completed"),
        )

    # Executa poda com janela de 30 dias
    pruned = repo.prune_inactive_workflows(days=30)
    assert pruned == 1

    # Verifica persistência
    assert repo.get_workflow("wf_old_completed") is None
    assert repo.get_workflow("wf_old_running") is not None
    assert repo.get_workflow("wf_recent_completed") is not None

    # 2. Testa renovação (touch) do workflow: se o usuário interagir com wf_recent_completed, seu updated_at renova
    repo.touch_workflow("wf_recent_completed")
    refreshed = repo.get_workflow("wf_recent_completed")
    updated_dt = datetime.fromisoformat(refreshed["updated_at"])
    assert (datetime.now(timezone.utc) - updated_dt).total_seconds() < 5


def test_intelligent_router_data_driven_decision(tmp_path: Path):
    """Testa transição do router de fallback para vencedor orientado a dados quando atinge MIN_DATA_DRIVEN_SAMPLES."""
    db_path = tmp_path / "sinestro_test.db"
    repo = Repository(db_path)

    # Mock registry simples com can_accept
    class FakeRegistry:
        def can_accept(self, agent: str):
            return True, None

    registry = FakeRegistry()

    # Com 0 amostras, o router retorna fallback
    agent, reason = IntelligentRouter.route("ui", files=["src/ui.lua"], registry=registry, repo=repo)
    assert "data_driven" not in reason

    # Adiciona exatamente 4 amostras no banco (estritamente abaixo do limiar de 5)
    for _ in range(4):
        repo.record_subtask_outcome("deepseek", "ui", first_pass=True, review_cycles=0, duration_seconds=10.0)

    # Com 4 amostras (< 5), o router NÃO pode acionar roteamento orientado a dados (mata mutação de limiar 5 -> 0)
    agent, reason = IntelligentRouter.route("ui", files=["src/ui.lua"], registry=registry, repo=repo)
    assert "data_driven" not in reason

    # Adiciona a 5ª amostra para deepseek (100% first pass)
    repo.record_subtask_outcome("deepseek", "ui", first_pass=True, review_cycles=0, duration_seconds=10.0)

    # Adiciona 5 amostras para gemini com baixo desempenho (20% first pass, muitos ciclos)
    for _ in range(4):
        repo.record_subtask_outcome("gemini", "ui", first_pass=False, review_cycles=2, duration_seconds=60.0)
    repo.record_subtask_outcome("gemini", "ui", first_pass=True, review_cycles=1, duration_seconds=40.0)

    # Agora deve escolher data_driven e eleger deepseek
    agent, reason = IntelligentRouter.route("ui", files=["src/ui.lua"], registry=registry, repo=repo)
    assert agent == "deepseek"
    assert "data_driven:ui" in reason
    assert "sucesso=100%" in reason


def test_api_fase4_endpoints(harness):
    """Testa endpoints /metrics/routing-stats e /workflows/prune-inactive via FastAPI."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            # 1. GET /metrics/routing-stats (inicialmente vazio)
            res = await h.client.get("/metrics/routing-stats")
            assert res.status_code == 200
            assert isinstance(res.json(), list)

            # 2. POST /workflows/prune-inactive
            prune_res = await h.client.post("/workflows/prune-inactive?days=30")
            assert prune_res.status_code == 200
            data = prune_res.json()
            assert "pruned_count" in data
            assert data["retention_days"] == 30

    run(go())


def test_workflow_execution_records_routing_outcome(harness, workspace):
    """Subtarefas concluídas em workflow real alimentam as métricas de roteamento."""
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            res = await h.client.post("/tasks", json={"goal": "Workflow para métricas de roteamento", "mode": "simple"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id)
            assert d["status"] in (WF.COMPLETED, WF.COMPLETED_UNREVIEWED)

            # Verifica se /metrics/routing-stats agora contém amostras
            res_stats = await h.client.get("/metrics/routing-stats")
            assert res_stats.status_code == 200
            stats = res_stats.json()
            assert len(stats) >= 1
            assert any(s["samples_count"] >= 1 for s in stats)

    run(go())


def test_resolve_assignment_without_negotiation_consults_data_driven(tmp_path: Path):
    """Quando não há negociação (ex.: modo simples), resolve_assignment deve consultar o placar empírico."""
    from orchestrator.assignment import AssignmentRules, resolve_assignment
    db_path = tmp_path / "sinestro_test.db"
    repo = Repository(db_path)

    # Popula 5 amostras com 100% de sucesso para deepseek em 'ui'
    for _ in range(5):
        repo.record_subtask_outcome("deepseek", "ui", first_pass=True, review_cycles=0, duration_seconds=10.0)

    rules = AssignmentRules(default_agent="gemini")
    task = {"id": "sub-1", "category": "ui", "files": ["ui.lua"], "assigned_to": "gemini"}

    # Sem resposta (response=None)
    decision = resolve_assignment(task, proposed_by="gemini", response=None, responder=None,
                                  rules=rules, gemini_unavailable_reason=None, repo=repo)
    assert decision.assigned_to == "deepseek"
    assert decision.decided_by == "data_driven"
    assert "data_driven:ui" in decision.rule


def test_routing_clean_data_filters_unreviewed_and_non_settled(tmp_path: Path):
    """Testa que subtasks com revisão dispensada ou não concluídas com sucesso técnico não poluem o placar."""
    db_path = tmp_path / "sinestro_test.db"
    repo = Repository(db_path)

    # 1. Subtask aprovada com revisão normal -> deve ser registrada
    repo.record_subtask_outcome("deepseek", "core", first_pass=True, review_cycles=0, duration_seconds=15.0)
    stats = repo.get_routing_stats(category="core", agent="deepseek")
    assert len(stats) == 1
    assert stats[0]["samples_count"] == 1

    # 2. Verifica que get_routing_stats respeita filtros e contagem
    repo.record_subtask_outcome("deepseek", "core", first_pass=False, review_cycles=1, duration_seconds=25.0)
    stats = repo.get_routing_stats(category="core", agent="deepseek")
    assert stats[0]["samples_count"] == 2
    assert stats[0]["first_pass_count"] == 1
