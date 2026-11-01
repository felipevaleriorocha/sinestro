"""[MOCK] GeminiAdapter contra um simulador de `agy` (tests/fakes/fake_agy.py). Não chama o modelo real."""
import json
import sys
import time

import pytest

from agents.base import (AgentAuthError, AgentResponseError, AgentTimeoutError, AgentUnavailableError, QuotaExceededError,
                         parse_json_object)
from agents.gemini_adapter import GeminiAdapter, parse_agy_output, result_from_payload
from database.repository import Repository
from tests.conftest import FAKE_AGY


def make(settings, repo=None):
    return GeminiAdapter(settings, command_prefix=[sys.executable, str(FAKE_AGY)], session_store=repo)


def test_parse_success_payload():
    p = parse_agy_output(json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "olá", "duration_seconds": 2,
                                     "num_turns": 1, "usage": {"input_tokens": 3}}))
    r = result_from_payload(p, "gemini-4-argon-high")
    assert (r.text, r.session_id, r.duration_seconds, r.num_turns, r.usage) == ("olá", "c1", 2.0, 1, {"input_tokens": 3})


def test_parse_tolerates_noise_before_json():
    out = '[warn] atualização disponível\n{"status": "SUCCESS", "response": "ok"}'
    assert parse_agy_output(out)["response"] == "ok"


def test_parse_errors():
    with pytest.raises(AgentResponseError):
        parse_agy_output("sem json")
    with pytest.raises(AgentResponseError):
        result_from_payload({"status": "ERROR", "error": "boom"}, "m")
    with pytest.raises(QuotaExceededError):
        result_from_payload({"status": "ERROR", "error": "429 Too Many Requests"}, "m")
    with pytest.raises(AgentResponseError):
        result_from_payload({"status": "SUCCESS", "response": ""}, "m")


def test_parse_json_object_from_model_text():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Segue o plano: {"tasks": [{"id": "T1"}]} fim') == {"tasks": [{"id": "T1"}]}


def test_initialization_and_health(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format")
    a = make(make_settings())
    assert a.model == "gemini-4-argon-high"
    h = a.health()
    assert h.available and h.status == "ok"
    assert h.checks["effort_flag"] is None and h.checks["resume_flag"] is None


def test_missing_binary_is_reported(make_settings):
    a = GeminiAdapter(make_settings(AGY_BIN="agy-que-nao-existe-xyz"))
    h = a.health()
    assert not h.available and h.status == "not_configured"
    with pytest.raises(AgentUnavailableError):
        a.run("oi", context_key="c", run_id="r")


def test_run_success_records_usage_and_uses_default_model(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "success")
    r = make(make_settings()).run("prompt curto", context_key="ctx", run_id="r1")
    args = json.loads(r.text)["args"]
    assert args[args.index("--model") + 1] == "gemini-4-argon-high"
    assert args[args.index("--output-format") + 1] == "json"
    assert r.usage == {"input_tokens": 10, "output_tokens": 5} and r.duration_seconds == 1.5


def test_effort_flag_only_when_confirmed_by_help(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format")
    r = make(make_settings(AGY_EFFORT="high")).run("x", context_key="c", run_id="r")
    assert "--effort" not in json.loads(r.text)["args"]
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format --effort")
    r = make(make_settings(AGY_EFFORT="high")).run("x", context_key="c", run_id="r")
    args = json.loads(r.text)["args"]
    assert args[args.index("--effort") + 1] == "high"


def test_conversation_id_preserved_only_with_confirmed_resume(make_settings, monkeypatch):
    s = make_settings()
    repo = Repository(s.db_path)
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format")
    make(s, repo).run("x", context_key="ctx1", run_id="r")
    assert repo.get_session("gemini", "ctx1") is None
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format --conversation")
    a = make(s, repo)
    a.run("x", context_key="ctx2", run_id="r")
    assert repo.get_session("gemini", "ctx2") == "conv-123"
    r = a.run("y", context_key="ctx2", run_id="r2")
    args = json.loads(r.text)["args"]
    assert args[args.index("--conversation") + 1] == "conv-123"


def test_error_status_quota_nonzero_and_invalid(make_settings, monkeypatch):
    a = make(make_settings())
    for mode, exc in (("error_status", AgentResponseError), ("quota", QuotaExceededError),
                      ("invalid_json", AgentResponseError), ("nonzero", AgentAuthError), ("empty", AgentResponseError)):
        monkeypatch.setenv("FAKE_AGY_MODE", mode)
        with pytest.raises(exc):
            a.run("x", context_key="c", run_id=f"r-{mode}")


def test_noisy_output_still_parsed(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "noisy")
    assert make(make_settings()).run("x", context_key="c", run_id="r").session_id == "conv-123"


def test_timeout_kills_process(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "sleep")
    a = make(make_settings(AGY_TIMEOUT_SECONDS=5))
    t0 = time.monotonic()
    with pytest.raises(AgentTimeoutError):
        a.run("x", context_key="c", run_id="r")
    assert time.monotonic() - t0 < 15


def test_long_prompt_goes_to_file_and_is_cleaned(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "success")
    s = make_settings(AGY_PROMPT_MODE="file")
    r = make(s).run("linha1\n\"aspas\" %PATH% & | <>", context_key="c", run_id="r-file")
    args = json.loads(r.text)["args"]
    prompt_arg = args[args.index("-p") + 1]
    assert "r-file.md" in prompt_arg and '"' not in prompt_arg and "%" not in prompt_arg
    assert not (s.runtime_dir / "prompts" / "r-file.md").exists()
