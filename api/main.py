"""Fábrica da aplicação FastAPI: REST + A2A (SDK oficial) + painel."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Callable

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agents.base import AgentBackend
from api.container import build_container
from api.routes import router
from communication.a2a_server import mount_a2a_agents
from core.config import BASE_DIR, Settings, load_settings
from core.logging_setup import register_secrets, setup_logging
from orchestrator.agent_registry import AGENT_DEFINITIONS

log = logging.getLogger("orchestrator")


def create_app(settings: Settings | None = None, backends: dict[str, AgentBackend] | None = None,
               a2a_in_process: bool = False, configure_logging: bool = True, checks=None) -> FastAPI:
    settings = settings or load_settings()
    register_secrets(settings.secrets())
    if configure_logging:
        setup_logging(settings.log_dir)

    holder: dict[str, FastAPI] = {}
    factory: Callable[[], httpx.AsyncClient] | None = None
    if a2a_in_process:
        # Usado nos testes: o cliente A2A fala com os servidores A2A pelo ASGI, sem abrir porta.
        factory = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=holder["app"]), base_url=settings.public_url,  # noqa: E731
                                            timeout=60)

    container = build_container(settings, backends, factory, checks)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await startup(app)
        yield
        await shutdown(app)

    app = FastAPI(title="Roblox Multi-Agent Orchestrator", version="0.5.0", lifespan=lifespan)
    holder["app"] = app
    app.state.container = container
    app.state.started = False
    app.include_router(router)
    app.state.cards = mount_a2a_agents(app, definitions=AGENT_DEFINITIONS, backends=container.backends, repo=container.repo,
                                       task_store=container.task_store, public_url=settings.public_url,
                                       domain=container.domain)
    dashboard_dir = BASE_DIR / "dashboard"
    if dashboard_dir.is_dir():
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/dashboard/")

    return app


async def startup(app: FastAPI) -> None:
    if app.state.started:
        return
    c = app.state.container
    interrupted = c.repo.mark_interrupted_workflows()
    if interrupted:
        log.warning("%s workflow(s) marcados como interrompidos após reinício.", interrupted)
    await c.task_store.initialize()
    c.locks.release_expired()
    app.state.started = True

    async def _warmup():
        for name in c.registry.names():
            try:
                await c.registry.check(name)
            except Exception as exc:  # noqa: BLE001
                log.warning("Health inicial de %s falhou: %s", name, exc)
    app.state.warmup = asyncio.create_task(_warmup())
    log.info("Orquestrador pronto em %s (mock=%s).", c.settings.public_url, c.mock_mode)


async def shutdown(app: FastAPI) -> None:
    c = app.state.container
    w = getattr(app.state, "warmup", None)
    if w and not w.done():
        w.cancel()
    await c.coordinator.shutdown()
    await c.a2a.close()
    await c.engine.dispose()
    for b in c.backends.values():
        close = getattr(b, "close", None)
        if close:
            close()
    app.state.started = False

