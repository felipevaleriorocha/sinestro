"""Testes das melhorias da Fase 1 (Confiabilidade, Cotas, Retries, Sanitização)."""
from datetime import datetime, timezone
import pytest

from agents.base import QuotaCooldown, parse_cooldown_seconds
from agents.mock_adapters import build_mock_backends
from communication.a2a_server import RoleAgentExecutor
from orchestrator.schemas import ImplementationReport, ReviewResult
from tests.conftest import run
from tests.unit.test_symmetric_flow import start, writer, task_for


def test_parse_cooldown_seconds_various_formats():
    # 36h57m
    assert parse_cooldown_seconds("Quota excedida, reseta em 36h57m.") == 36 * 3600 + 57 * 60
    # 14 horas
    assert parse_cooldown_seconds("reseta em 14h") == 14 * 3600
    # 45 minutos
    assert parse_cooldown_seconds("bloqueado por excesso de requisições, tente em 45 minutos") == 45 * 60
    # 2 dias 5 horas
    assert parse_cooldown_seconds("reseta em 2d 5h") == 2 * 86400 + 5 * 3600
    # retry-after header pattern
    assert parse_cooldown_seconds("Rate limit reached. retry-after: 180") == 180
    # Fallback para default quando texto não contém tempo reconhecível
    assert parse_cooldown_seconds("Sem cota disponível no momento.", default_seconds=1200) == 1200


def test_quota_cooldown_marks_exact_calculated_time(tmp_path):
    state_file = tmp_path / "gemini_state.json"
    qc = QuotaCooldown(state_file, model="gemini-test", default_minutes=60)
    
    # Mensagem com 36h57m
    qc.mark("Quota excedida, reseta em 36h57m")
    
    data = qc._read()
    quota = data.get("quota")
    assert quota is not None
    assert quota["seconds"] == 36 * 3600 + 57 * 60
    assert quota["model"] == "gemini-test"
    
    # Reason deve acusar bloqueio
    assert qc.reason() is not None
    assert "liberado em ~36h" in qc.reason()


def test_sanitization_prevents_raw_text_leak_in_implementation():
    raw_leaked_thought = "Eu pensei em alterar o arquivo X e acho que você deve usar a função Y..."
    # Quando a resposta do modelo não é JSON válido
    rep, summary = RoleAgentExecutor._structure("implement_task", raw_leaked_thought)
    
    # O summary NÃO deve ser o texto bruto do modelo
    assert raw_leaked_thought not in summary
    assert "não retornou formato JSON estruturado" in summary
    
    # O texto bruto fica isolado em raw_text para auditoria, sem contaminar o prompt seguinte
    assert rep.get("raw_text") == raw_leaked_thought
    assert rep.get("structured") is False


def test_sanitization_prevents_raw_text_leak_in_review():
    raw_leaked_review = "Estou divagando sobre o código antes de emitir o veredito..."
    rev, summary = RoleAgentExecutor._structure("review_implementation", raw_leaked_review)
    
    assert raw_leaked_review not in summary
    assert "não retornou formato JSON estruturado" in summary
    assert rev.get("raw_text") == raw_leaked_review
    assert rev.get("structured") is False


def test_planner_fallback_on_invalid_response(harness, workspace):
    """Quando o Gemini devolve resposta inválida/vazia no plano, o orquestrador não aborta; DeepSeek assume via fallback."""
    async def go():
        b = build_mock_backends()
        # Gemini responder retorna texto não JSON no plan_feature (provocando invalid_response)
        def bad_gemini_responder(skill, prompt, options):
            if skill == "plan_feature":
                return "resposta vazia ou não estruturada"
            return None
        b["gemini"].responder = bad_gemini_responder
        b["deepseek"].responder = writer("deepseek", workspace, tasks=[task_for("deepseek", "ui")])

        async with harness(b) as h:
            d = await start(h)
            # Deve ter assumido o plano via fallback
            assert d.get("planned_by") == "deepseek"
            assert d["planned_by_fallback"] is True
            # Deve registrar evento de planner_fallback
            events = [e["event"] for e in d["events"]]
            assert "planner_fallback" in events
    run(go())
