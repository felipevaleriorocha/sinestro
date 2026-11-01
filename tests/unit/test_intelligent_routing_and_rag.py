"""Testes unitários e de integração para a Fase 4:
- Banco vetorial local (VectorStore) e busca semântica (RAG).
- Indexação e enriquecimento de contexto no ContextManager.
- Roteamento inteligente baseado em capacidades e otimização de custo (IntelligentRouter).
- Agregação de custos e métricas de modelos.
- Endpoints da API (/providers, /metrics/costs, /context/search, /context/index).
"""
import asyncio
from pathlib import Path

import pytest

from core.capabilities import CapabilityRegistry, ModelCapability, ModelSpec
from memory.context_manager import ContextManager
from memory.decision_store import DecisionStore
from memory.vector_store import HashedDenseEmbedding, SearchResult, VectorStore, chunk_text
from orchestrator.agent_registry import AGENT_DEFINITIONS, AgentRegistry
from orchestrator.assignment import AssignmentRules, IntelligentRouter, resolve_assignment
from orchestrator.schemas import AssignmentResponseItem


def test_chunk_text_splits_and_overlaps():
    text = "Primeiro parágrafo curto.\n\nSegundo parágrafo com mais detalhes sobre a arquitetura do sistema.\n\nTerceiro parágrafo sobre segurança."
    chunks = chunk_text(text, chunk_size=50, chunk_overlap=10)
    assert len(chunks) >= 3
    assert all(isinstance(c, str) and len(c) > 0 for c in chunks)


def test_hashed_dense_embedding_normalized():
    embedder = HashedDenseEmbedding(dimension=64)
    vectors = embedder.embed(["Sistema de persistência DataStore", "Interface de usuário HUD"])
    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == 64
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-4


def test_vector_store_add_search_delete(tmp_path):
    db_file = tmp_path / "test_vec.db"
    vs = VectorStore(db_file, dimension=64)

    vs.add_document("doc_datastore", "Implementação do sistema de persistência com DataStore e retry.", metadata={"cat": "core"})
    vs.add_document("doc_hud", "Sistema de interface gráfica GUI e animações no cliente.", metadata={"cat": "ui"})

    results = vs.search("persistência e salvamento de dados", top_k=2)
    assert len(results) >= 1
    assert results[0].doc_id == "doc_datastore"
    assert results[0].score > 0.1

    # Busca com filtro de metadata
    filtered = vs.search("interface", top_k=2, filter_metadata={"cat": "ui"})
    assert len(filtered) == 1
    assert filtered[0].doc_id == "doc_hud"

    # Remoção
    vs.delete_document("doc_datastore")
    assert vs.count() == 1
    vs.close()


def test_context_manager_rag_indexing_and_augmented(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# Projeto Teste\nDocumentação geral sobre arquitetura e regras de rede.", encoding="utf-8")
    (project_dir / "AGENTS.md").write_text("# Papéis\nGemini cuida de backend e DeepSeek de assets.", encoding="utf-8")

    cm = ContextManager(project_dir, vector_store=VectorStore(":memory:"))
    count = cm.index_workspace()
    assert count >= 2

    # Busca semântica
    res = cm.search_relevant("regras de rede e arquitetura", top_k=3)
    assert len(res) >= 1
    assert any("README.md" in r.doc_id for r in res)

    # Augmented bundle
    bundle = cm.build_augmented("arquitetura e regras de rede")
    assert "RELEVÂNCIA SEMÂNTICA (RAG)" in bundle.text
    assert bundle.hash != ""


def test_intelligent_router_capability_and_cost():
    class DummyBackend:
        def __init__(self, name: str, model: str):
            self.name = name
            self.model = model
            self.kind = "mock"

        def unavailable_reason(self):
            return None

    cap_reg = CapabilityRegistry()
    m_gemini = ModelSpec(
        id="gemini-model",
        name="Gemini High",
        provider_id="gemini",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODE, ModelCapability.REASONING}),
        cost_per_million_input=1.25,
        cost_per_million_output=5.00,
        cost_tier="high",
    )
    m_deepseek = ModelSpec(
        id="deepseek-model",
        name="DeepSeek Fast",
        provider_id="deepseek",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODE}),
        cost_per_million_input=0.27,
        cost_per_million_output=1.10,
        cost_tier="low",
    )
    cap_reg.register_model(m_gemini)
    cap_reg.register_model(m_deepseek)

    backends = {
        "gemini": DummyBackend("gemini", "gemini-model"),
        "deepseek": DummyBackend("deepseek", "deepseek-model"),
    }
    registry = AgentRegistry(backends, AGENT_DEFINITIONS, capability_registry=cap_reg)

    # Tarefa de raciocínio profundo / arquitetura -> deve escolher Gemini
    agent, rule = IntelligentRouter.route("architecture", [], registry)
    assert agent == "gemini"
    assert "reasoning" in rule

    # Tarefa de asset / boilerplate -> deve escolher DeepSeek por custo-eficiência
    agent, rule = IntelligentRouter.route("asset", [], registry)
    assert agent == "deepseek"
    assert "cost_efficiency" in rule


def test_resolve_assignment_with_intelligent_router():
    rules = AssignmentRules(
        default_agent="gemini",
        categories={"security": "gemini"},
        path_rules=[],
    )

    class DummyBackend:
        def __init__(self, name: str, model: str):
            self.name = name
            self.model = model
            self.kind = "mock"

        def unavailable_reason(self):
            return None

    cap_reg = CapabilityRegistry()
    cap_reg.register_model(ModelSpec(
        id="gemini-m", name="Gemini", provider_id="gemini",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODE, ModelCapability.REASONING}),
        cost_per_million_input=1.25, cost_tier="high",
    ))
    cap_reg.register_model(ModelSpec(
        id="deepseek-m", name="DeepSeek", provider_id="deepseek",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODE}),
        cost_per_million_input=0.27, cost_tier="low",
    ))
    registry = AgentRegistry(
        {"gemini": DummyBackend("gemini", "gemini-m"), "deepseek": DummyBackend("deepseek", "deepseek-m")},
        capability_registry=cap_reg,
    )

    task_asset = {"id": "T1", "category": "asset", "assigned_to": "gemini", "assignment_reason": "proposta inicial"}
    counter_response = AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="deepseek", reason="melhor custo")

    # Quando contestado, o IntelligentRouter seleciona DeepSeek para asset
    decision = resolve_assignment(task_asset, "gemini", counter_response, "deepseek", rules, None, registry=registry)
    assert decision.assigned_to == "deepseek"
    assert decision.reviewer == "gemini"
    assert decision.decided_by == "intelligent_routing"


def test_cost_summary_and_providers_endpoint(harness, workspace):
    from agents.mock_adapters import build_mock_backends
    from tests.conftest import run
    from tests.unit.test_final_review import impl_writer, start

    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            # Verifica endpoint /providers
            prov_res = await h.client.get("/providers")
            assert prov_res.status_code == 200
            providers = prov_res.json()
            assert len(providers) >= 2
            assert any(p["name"] == "gemini" or p["id"] in {"gemini", "mock-gemini"} for p in providers)
            assert any(p["name"] == "deepseek" or p["id"] in {"deepseek", "mock-deepseek"} for p in providers)

            # Verifica endpoint /metrics/costs
            cost_res = await h.client.get("/metrics/costs")
            assert cost_res.status_code == 200
            costs = cost_res.json()
            assert "total_runs" in costs
            assert "by_agent" in costs

            # Verifica busca semântica /context/search
            search_res = await h.client.get("/context/search?q=agents&top_k=2")
            assert search_res.status_code == 200

    run(go())
