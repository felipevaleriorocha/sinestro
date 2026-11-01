"""Montagem dos serviços (injeção de dependências simples)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

import httpx
from a2a.server.tasks import DatabaseTaskStore
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents.base import AgentBackend
from agents.gemini_adapter import GeminiAdapter
from agents.opencode_adapter import OpenCodeAdapter
from communication.a2a_client import A2AAgentClient
from communication.a2a_server import agent_base_path
from core.config import Settings
from database.repository import Repository
from mcp_bridge.server_registry import MCPServerRegistry
from memory.context_manager import ContextManager
from memory.decision_store import DecisionStore
from orchestrator.agent_registry import AGENT_DEFINITIONS, AgentRegistry
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


def build_container(settings: Settings, backends: dict[str, AgentBackend] | None = None,
                    http_client_factory: Callable[[], httpx.AsyncClient] | None = None) -> Container:
    repo = Repository(settings.db_path)
    mock_mode = False
    if backends is None:
        if os.getenv("ORCH_MOCK_AGENTS", "").lower() in {"1", "true", "yes", "sim"}:
            from agents.mock_adapters import build_mock_backends
            backends = build_mock_backends(delay=1.0)
            mock_mode = True
            log.warning("ORCH_MOCK_AGENTS ativo: agentes SIMULADOS. Nada aqui é integração real.")
        else:
            backends = {
                "gemini": GeminiAdapter(settings, session_store=repo),
                "deepseek": OpenCodeAdapter(settings, session_store=repo),
            }
    else:
        mock_mode = any(getattr(b, "kind", "") == "mock" for b in backends.values())

    # Arquivo separado: o DatabaseTaskStore (aiosqlite) e o Repository (sqlite3 síncrono) no mesmo arquivo
    # disputam o lock de escrita e podem travar o event loop. Ambos ficam ao lado, persistentes.
    a2a_db = settings.db_path.with_name(settings.db_path.stem + "_a2a.db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{a2a_db.as_posix()}")
    task_store = DatabaseTaskStore(engine)  # tabela padrão do SDK ("tasks") dentro de orchestrator_a2a.db
    decisions = DecisionStore(repo)
    context = ContextManager(settings.project_root, settings.context_sources_file, decisions,
                             settings.context_max_chars, settings.context_file_max_chars)
    locks = FileLockManager(repo, settings.project_root, settings.file_lock_ttl_seconds)
    git = GitInspector(settings.project_root)
    mcp = MCPServerRegistry(settings.mcp_config_file)
    tasks = TaskManager(repo)
    registry = AgentRegistry(backends, AGENT_DEFINITIONS, allow_fallback=settings.allow_agent_fallback)
    agent_urls = {name: f"{settings.public_url}{agent_base_path(name)}" for name in backends}
    a2a = A2AAgentClient(repo, agent_urls, http_client_factory=http_client_factory,
                         poll_interval=settings.a2a_poll_interval, task_timeout=settings.a2a_task_timeout)
    coordinator = Coordinator(settings, repo, tasks, registry, a2a, context, decisions, locks, git, mcp)
    return Container(settings, repo, backends, mock_mode, engine, task_store, tasks, registry, a2a, context, decisions,
                     locks, git, mcp, coordinator)
