"""[REAL] Testes contra Antigravity CLI e servidor OpenCode reais.

Desligados por padrão. Rodar no Windows, com OpenCode iniciado (.\\run_opencode.ps1):
  $env:RUN_LIVE_TESTS = "1";        .\\.venv\\Scripts\\python.exe -m pytest tests/live -v     (sem tokens)
  $env:RUN_LIVE_TOKEN_TESTS = "1";  (adicional) envia UM prompt curto ao Gemini e, se houver cota, ao DeepSeek.
"""
import os

import pytest

from agents.base import ContractNotVerifiedError, QuotaExceededError
from agents.gemini_adapter import GeminiAdapter
from agents.opencode_adapter import OpenCodeAdapter
from core.config import load_settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="defina RUN_LIVE_TESTS=1 para testes reais"),
]
TOKENS = os.getenv("RUN_LIVE_TOKEN_TESTS") == "1"


@pytest.fixture(scope="module")
def settings():
    return load_settings()


def test_real_agy_reachable_without_tokens(settings):
    h = GeminiAdapter(settings).health(deep=True)
    assert h.available, h.detail


def test_real_opencode_server_and_model_recognized(settings):
    a = OpenCodeAdapter(settings)
    try:
        assert a.info() is not None
        st = a.model_status(refresh=True)
        assert st["found"], f"{settings.opencode_model} não aparece em /api/model"
        print("modelo:", st, "agentes:", a.agents(), "contrato:", a.discover_spec())
    finally:
        a.close()


@pytest.mark.skipif(not TOKENS, reason="consome tokens; defina RUN_LIVE_TOKEN_TESTS=1")
def test_real_gemini_prompt(settings):
    r = GeminiAdapter(settings).run('Responda somente com o JSON {"ok": true}', context_key="live-test", run_id="live-gemini")
    assert r.text and r.raw_status == "SUCCESS"
    print("uso:", r.usage, "duração:", r.duration_seconds, "conversation_id:", r.session_id)


@pytest.mark.skipif(not TOKENS, reason="consome tokens; defina RUN_LIVE_TOKEN_TESTS=1")
def test_real_deepseek_prompt_or_quota(settings):
    a = OpenCodeAdapter(settings)
    try:
        r = a.run('Responda somente com o JSON {"ok": true}. Não edite arquivos.', context_key="live-test", run_id="live-ds")
        assert r.text
    except QuotaExceededError as exc:
        pytest.skip(f"Quota do DeepSeek esgotada: {exc}")
    except ContractNotVerifiedError as exc:
        pytest.fail(f"Rotas de sessão do OpenCode não confirmadas: {exc}")
    finally:
        a.close()
