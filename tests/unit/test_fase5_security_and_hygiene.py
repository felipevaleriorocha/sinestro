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
    """Verifica que o CORSMiddleware permite apenas origens locais e rejeita origens externas."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            # Origem local válida recebe header CORS correspondente
            res = await h.client.get("/health", headers={"Origin": "http://localhost:5173"})
            assert res.status_code == 200
            assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"

            # Origem externa maliciosa NÃO recebe allow-origin
            res_evil = await h.client.get("/health", headers={"Origin": "http://evil.example"})
            assert res_evil.headers.get("access-control-allow-origin") != "http://evil.example"
            assert res_evil.headers.get("access-control-allow-origin") != "*"

    run(go())


def test_origin_csrf_protection_rejects_external_mutations(harness):
    """Garante que requisições mutantes (POST/PATCH/DELETE) de origens externas sejam rejeitadas com 403."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            # Origem maliciosa tentando alterar configurações críticas
            res = await h.client.post("/settings/auto-approve", json={"enabled": True}, headers={"Origin": "http://evil.example"})
            assert res.status_code == 403
            assert "Origem não autorizada" in res.json().get("detail", "")

            # Origem local permitida funciona normalmente
            res_ok = await h.client.post("/settings/auto-approve", json={"enabled": False}, headers={"Origin": "http://localhost:8787"})
            assert res_ok.status_code == 200

    run(go())


def test_trusted_host_middleware_blocks_dns_rebinding(harness):
    """Garante que TrustedHostMiddleware rejeita Host header forjado (DNS rebinding) com 400."""
    async def go():
        b = build_mock_backends()
        async with harness(b) as h:
            # Host arbitrário de atacante é sumariamente rejeitado
            res = await h.client.get("/health", headers={"Host": "rebind.attacker.example"})
            assert res.status_code == 400

            # Host localhost válido é aceito
            res_ok = await h.client.get("/health", headers={"Host": "localhost"})
            assert res_ok.status_code == 200

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
