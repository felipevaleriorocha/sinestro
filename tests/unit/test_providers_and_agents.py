"""Testes unitários da Fase 2: Abstração de Provedores e Agentes.

Valida:
1. ModelCapability e ModelSpec (capacidades heterogêneas).
2. CapabilityRegistry (registro e consulta de capacidades).
3. Contrato da entidade Provider (MockProvider, GeminiProvider, OpenCodeProvider).
4. Contrato da entidade Agent (Persona + Skills + Model/Provider binding).
5. Seleção e filtragem por capacidades no AgentRegistry.
"""
from __future__ import annotations

from agents.base import AgentBackend, AgentResult
from agents.mock_adapters import MockProvider
from core.agent import Agent, AgentDefinition, SkillDef
from core.capabilities import CapabilityRegistry, ModelCapability, ModelSpec
from core.providers import Provider
from orchestrator.agent_registry import AgentRegistry


def test_model_capabilities_and_specs():
    spec = ModelSpec(
        id="model-advanced",
        name="Advanced Vision Reasoner",
        provider_id="mock_prov",
        capabilities=frozenset({
            ModelCapability.TEXT,
            ModelCapability.CODE,
            ModelCapability.REASONING,
            ModelCapability.VISION,
        }),
        context_window=200_000,
    )
    assert spec.id == "model-advanced"
    assert spec.supports(ModelCapability.REASONING)
    assert spec.supports("vision")
    assert spec.supports("code")
    assert not spec.supports(ModelCapability.TOOL_CALLING)
    assert not spec.supports("invalid_capability")


def test_capability_registry():
    registry = CapabilityRegistry()
    m_reasoning = ModelSpec(
        id="m-r1",
        name="R1 Reasoning",
        provider_id="prov-a",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.REASONING}),
    )
    m_code = ModelSpec(
        id="m-c1",
        name="C1 Coder",
        provider_id="prov-b",
        capabilities=frozenset({ModelCapability.TEXT, ModelCapability.CODE}),
    )
    registry.register_model(m_reasoning)
    registry.register_model(m_code)

    assert registry.get_model("m-r1") == m_reasoning
    assert registry.get_model("m-unknown") is None
    assert registry.has_capability("m-r1", ModelCapability.REASONING)
    assert not registry.has_capability("m-c1", ModelCapability.REASONING)
    assert registry.has_capability("m-c1", "code")

    reasoning_models = registry.find_models(ModelCapability.REASONING)
    assert len(reasoning_models) == 1
    assert reasoning_models[0].id == "m-r1"

    code_models = registry.find_models("code")
    assert len(code_models) == 1
    assert code_models[0].id == "m-c1"


def test_mock_provider_conformance():
    prov = MockProvider("test-mock", "test-model", delay=0.0)
    assert isinstance(prov, Provider)
    assert prov.default_model == "test-model"
    models = prov.get_models()
    assert len(models) == 1
    assert models[0].id == "test-model"
    assert prov.supports_capability(ModelCapability.REASONING)

    health = prov.health()
    assert health.available
    assert health.status == "ok"

    res = prov.execute("Olá", context_key="ctx-1", run_id="r-1", options={"skill": "plan_feature"})
    assert isinstance(res, AgentResult)
    assert len(prov.calls) == 1
    assert prov.calls[0]["skill"] == "plan_feature"

    prov.cancel("r-1")
    assert "r-1" in prov._cancel


def test_agent_entity_persona_skills_and_execution():
    prov = MockProvider("agent-mock", "gemini-4-argon-high", delay=0.0)
    skill_a = SkillDef("plan", "Planejamento", "Planeja arquitetura")
    skill_b = SkillDef("review", "Revisão", "Revisa código")

    agent = Agent(
        name="lead_architect",
        display_name="Lead Architect",
        role="Principal Architect",
        description="Especialista em design de sistemas resilientes",
        skills=(skill_a, skill_b),
        provider=prov,
        model="gemini-4-argon-high",
        required_capabilities=frozenset({ModelCapability.TEXT, ModelCapability.REASONING}),
    )

    # Verifica persona e identidade
    assert agent.name == "lead_architect"
    assert agent.display_name == "Lead Architect"
    assert agent.role == "Principal Architect"
    assert agent.skill_ids() == {"plan", "review"}
    assert agent.supports_skill("plan")
    assert not agent.supports_skill("implement")

    # Verifica compatibilidade com AgentBackend
    assert isinstance(agent, AgentBackend)
    assert agent.model == "gemini-4-argon-high"

    # Execução delega ao Provider com persona injetada nas options
    res = agent.run("Construa a arquitetura", context_key="ctx-arch", run_id="run-1", options={"skill": "plan"})
    assert isinstance(res, AgentResult)
    assert len(prov.calls) == 1
    call = prov.calls[0]
    assert call["skill"] == "plan"
    assert call["context_key"] == "ctx-arch"

    # Health delega e preserva nome do agente
    h = agent.health()
    assert h.available
    assert h.name == "lead_architect"
    assert h.model == "gemini-4-argon-high"

    # Exportação para dicionário e definição
    d = agent.to_dict()
    assert d["role"] == "Principal Architect"
    assert d["skills"] == ["plan", "review"]
    defn = agent.definition
    assert isinstance(defn, AgentDefinition)
    assert defn.name == "lead_architect"


def test_agent_registry_capability_filtering():
    prov_reasoning = MockProvider("reasoner", "model-reasoning")
    prov_simple = MockProvider("simple", "model-simple")
    # Altera capacidades do modelo simples para apenas TEXT
    prov_simple.get_models = lambda: [
        ModelSpec(id="model-simple", name="Simple", provider_id="simple", capabilities=frozenset({ModelCapability.TEXT}))
    ]

    skill = SkillDef("task_x", "Task X", "Faz algo")
    agent_r = Agent(
        name="reasoner_agent",
        display_name="Reasoner",
        role="Architect",
        description="",
        skills=(skill,),
        provider=prov_reasoning,
        model="model-reasoning",
    )
    agent_s = Agent(
        name="simple_agent",
        display_name="Simple",
        role="Junior",
        description="",
        skills=(skill,),
        provider=prov_simple,
        model="model-simple",
    )

    backends = {"reasoner_agent": agent_r, "simple_agent": agent_s}
    reg = CapabilityRegistry()
    reg.register_provider(prov_reasoning)
    reg.register_provider(prov_simple)

    registry = AgentRegistry(backends, allow_fallback=True, capability_registry=reg)

    # 1. Sem exigência de capacidade específica: o preferido aceita
    sel, err = registry.select("simple_agent", "task_x")
    assert sel == "simple_agent"
    assert err is None

    # 2. Exigindo REASONING: simple_agent não satisfaz, faz fallback para reasoner_agent
    sel, err = registry.select("simple_agent", "task_x", required_capabilities=frozenset({ModelCapability.REASONING}))
    assert sel == "reasoner_agent"
    assert err is None

    # 3. Exigindo capacidade que nenhum tem (ex.: VISION se removida)
    sel, err = registry.select("reasoner_agent", "task_x", required_capabilities=frozenset({ModelCapability.TOOL_CALLING}))
    assert sel is None
    assert "faltam capacidades" in err


def test_agent_definition_to_agent_conversion():
    prov = MockProvider("mock_prov", "default-model")
    defn = AgentDefinition(
        name="test_worker",
        display_name="Test Worker",
        role="Worker",
        description="Faz tarefas de teste",
        skills=(SkillDef("s1", "S1", "desc"),),
        provider="Mock Org",
    )
    agent = defn.to_agent(prov, model="custom-model")
    assert agent.name == "test_worker"
    assert agent.model == "custom-model"
    assert agent.provider == prov
    assert agent.supports_skill("s1")
