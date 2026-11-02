"""Montagem dos serviços (injeção de dependências simples)."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import httpx
from a2a.server.tasks import DatabaseTaskStore
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents.base import AgentBackend
from agents.gemini_adapter import GeminiAdapter, GeminiProvider
from agents.opencode_adapter import OpenCodeAdapter, OpenCodeProvider
from communication.a2a_client import A2AAgentClient
from communication.a2a_server import agent_base_path
from core.agent import Agent
from core.capabilities import CapabilityRegistry, ModelCapability
from core.config import Settings, update_env_variable
from core.domain import DomainProfile
from core.providers import Provider
from database.repository import Repository
from domains.roblox import RobloxDomainProfile
from mcp_bridge.server_registry import MCPServerRegistry
from memory.context_manager import ContextManager
from memory.decision_store import DecisionStore
from memory.vector_store import VectorStore
from orchestrator.agent_registry import AGENT_DEFINITIONS, AgentRegistry
from orchestrator.assignment import AssignmentRules
from orchestrator.checks import ChecksRunner
from orchestrator.coordinator import Coordinator
from orchestrator.file_locks import FileLockManager
from orchestrator.git_integration import GitInspector
from orchestrator.task_manager import TaskManager

log = logging.getLogger("orchestrator.container")


@dataclass
class Container:
    settings: Settings
    repo: Repository
    backends: dict[str, AgentBackend]
    mock_mode: bool
    engine: AsyncEngine
    task_store: DatabaseTaskStore
    tasks: TaskManager
    registry: AgentRegistry
    a2a: A2AAgentClient
    context: ContextManager
    decisions: DecisionStore
    locks: FileLockManager
    git: GitInspector
    mcp: MCPServerRegistry
    coordinator: Coordinator
    checks: ChecksRunner
    domain: DomainProfile
    providers: dict[str, Provider] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    capability_registry: CapabilityRegistry | None = None


def build_container(
    settings: Settings,
    backends: dict[str, AgentBackend] | None = None,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    checks: ChecksRunner | None = None,
    domain: DomainProfile | None = None,
) -> Container:
    repo = Repository(settings.db_path)
    mock_mode = False
    capability_registry = CapabilityRegistry()
    providers: dict[str, Provider] = {}

    if backends is None:
        if settings.mock_agents:
            from agents.mock_adapters import build_mock_backends
            mock_backends = build_mock_backends(delay=1.0)
            backends = mock_backends
            providers = {n: b for n, b in mock_backends.items()}
            mock_mode = True
            log.warning("ORCH_MOCK_AGENTS ativo: agentes SIMULADOS. Nada aqui é integração real.")
        else:
            gem_prov = GeminiProvider(settings, session_store=repo)
            open_prov = OpenCodeProvider(settings, session_store=repo)
            providers = {"gemini": gem_prov, "deepseek": open_prov}
            backends = {
                "gemini": GeminiAdapter(settings, session_store=repo),
                "deepseek": OpenCodeAdapter(settings, session_store=repo),
            }
    else:
        mock_mode = any(getattr(b, "kind", "") == "mock" for b in backends.values())
        for n, b in backends.items():
            if isinstance(b, Provider):
                providers[n] = b

    for p in providers.values():
        capability_registry.register_provider(p)

    domain = domain or RobloxDomainProfile(settings)

    # Arquivo separado: o DatabaseTaskStore (aiosqlite) e o Repository (sqlite3 síncrono) no mesmo arquivo
    # disputam o lock de escrita e podem travar o event loop. Ambos ficam ao lado, persistentes.
    a2a_db = settings.db_path.with_name(settings.db_path.stem + "_a2a.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{a2a_db.as_posix()}")
    task_store = DatabaseTaskStore(engine)  # tabela padrão do SDK ("tasks") dentro de orchestrator_a2a.db
    decisions = DecisionStore(repo)
    context_data = domain.load_context_sources()
    vector_store = VectorStore(settings.vector_store_file)
    context = ContextManager(
        settings.project_root,
        settings.context_sources_file,
        decisions,
        settings.context_max_chars,
        settings.context_file_max_chars,
        sources_config=context_data,
        vector_store=vector_store,
    )
    locks = FileLockManager(repo, settings.project_root, settings.file_lock_ttl_seconds)
    git = GitInspector(settings.project_root)
    mcp = MCPServerRegistry(settings.mcp_config_file)
    tasks = TaskManager(repo)
    agent_defs = domain.get_agent_definitions() or AGENT_DEFINITIONS

    agents: dict[str, Agent] = {}
    for name, defn in agent_defs.items():
        prov = providers.get(name)
        if prov:
            caps = (
                frozenset({ModelCapability.TEXT, ModelCapability.CODE, ModelCapability.REASONING})
                if name == "gemini"
                else frozenset({ModelCapability.TEXT, ModelCapability.CODE})
            )
            agents[name] = Agent(
                name=defn.name,
                display_name=defn.display_name,
                role=defn.role,
                description=defn.description,
                skills=defn.skills,
                provider=prov,
                model=prov.default_model,
                required_capabilities=caps,
            )

    registry = AgentRegistry(
        backends,
        agent_defs,
        allow_fallback=settings.allow_agent_fallback,
        capability_registry=capability_registry,
    )
    agent_urls = {name: f"{settings.public_url}{agent_base_path(name)}" for name in backends}
    a2a = A2AAgentClient(
        repo,
        agent_urls,
        http_client_factory=http_client_factory,
        poll_interval=settings.a2a_poll_interval,
        task_timeout=settings.a2a_task_timeout,
    )
    checks_data = domain.load_checks_config()
    allowed_tools = frozenset(checks_data["allowed_tools"]) if "allowed_tools" in checks_data else None
    checks = checks or ChecksRunner(
        settings.project_root,
        settings.checks_file,
        config=checks_data,
        allowed_tools=allowed_tools,
    )
    rules_data = domain.load_assignment_rules()
    rules = AssignmentRules.from_dict(rules_data) if rules_data else AssignmentRules.load(settings.assignment_rules_file)
    coordinator = Coordinator(
        settings,
        repo,
        tasks,
        registry,
        a2a,
        context,
        decisions,
        locks,
        git,
        mcp,
        checks=checks,
        rules=rules,
        domain=domain,
    )
    return Container(
        settings,
        repo,
        backends,
        mock_mode,
        engine,
        task_store,
        tasks,
        registry,
        a2a,
        context,
        decisions,
        locks,
        git,
        mcp,
        coordinator,
        checks,
        domain,
        providers=providers,
        agents=agents,
        capability_registry=capability_registry,
    )


def update_auto_approve_permissions(container: Container, enabled: bool) -> Settings:
    """Atualiza o parâmetro agy_auto_approve_permissions em memória e persiste no .env."""
    update_env_variable("AGY_AUTO_APPROVE_PERMISSIONS", "true" if enabled else "false")
    os.environ["AGY_AUTO_APPROVE_PERMISSIONS"] = "true" if enabled else "false"

    new_settings = replace(container.settings, agy_auto_approve_permissions=enabled)
    container.settings = new_settings
    if hasattr(container, "coordinator") and container.coordinator is not None:
        container.coordinator.s = new_settings
    gemini_backend = container.backends.get("gemini")
    if gemini_backend is not None and hasattr(gemini_backend, "settings"):
        gemini_backend.settings = new_settings
    return new_settings

