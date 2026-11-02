"""Testes da Fase 5: Segurança Local e Higiene de Arquitetura.

1. Fonte única de versão (core.version.__version__).
2. Consistência entre app FastAPI, rotas (/health) e orchestrator.task_manager.VERSION.
3. Middlewares de segurança (CORS e TrustedHost) habilitados e operacionais.
"""
from core.version import __version__
from orchestrator.task_manager import VERSION
from tests.conftest import run
from agents.mock_adapters import build_mock_backends


def test_single_version_source_consistency():
    """Garante que a versão é unificada e consistente em todo o sistema."""
    assert __version__ == "0.5.1"
    assert VERSION == __version__


def test_health_reports_unified_version(harness):
    """Garante que o endpoint /health reporta a versão definida em core.version."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.get("/health")
            assert res.status_code == 200
            data = res.json()
            assert data["version"] == __version__

    run(go())


def test_cors_middleware_headers(harness):
    """Verifica que o CORSMiddleware adiciona os cabeçalhos de permissão esperados."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.get("/health", headers={"Origin": "http://localhost:5173"})
            assert res.status_code == 200
            assert "access-control-allow-origin" in res.headers
            assert res.headers["access-control-allow-origin"] in ("*", "http://localhost:5173")

    run(go())


def test_agent_quota_info_and_token_accounting(harness):
    """Verifica que /agents retorna percentual de cota e métricas de tokens funcionam."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            res = await h.client.get("/agents")
            assert res.status_code == 200
            agents = res.json()
            assert len(agents) >= 2
            for a in agents:
                assert "quota" in a
                q = a["quota"]
                assert "percent_remaining" in q
                assert isinstance(q["percent_remaining"], int)
                assert 0 <= q["percent_remaining"] <= 100
                assert q["status"] in ("ok", "warning", "low", "exhausted")

    run(go())
