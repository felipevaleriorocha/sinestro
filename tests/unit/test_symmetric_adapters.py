"""[MOCK] Adaptadores simétricos: modo somente leitura, --add-dir, cooldown de cota do Gemini, agente Plan do OpenCode."""
import json
import sys

import httpx
import pytest

from agents.base import QuotaExceededError
from agents.gemini_adapter import GeminiAdapter
from agents.opencode_adapter import OpenCodeAdapter
from database.repository import Repository
from tests.conftest import FAKE_AGY
from tests.unit.test_opencode_adapter import FakeOpenCode

HELP_REAL = "-p --model --output-format --effort --conversation --mode --add-dir --sandbox"


def gem(settings):
    return GeminiAdapter(settings, command_prefix=[sys.executable, str(FAKE_AGY)])


def args_of(result):
    return json.loads(result.text)["args"]


def test_gemini_review_uses_plan_mode_only_when_supported(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", HELP_REAL)
    a = gem(make_settings())
    review = args_of(a.run("x", context_key="c", run_id="r1", options={"skill": "review_implementation", "read_only": True}))
    assert review[review.index("--mode") + 1] == "plan"
    impl = args_of(a.run("x", context_key="c", run_id="r2", options={"skill": "implement_task", "read_only": False}))
    assert "--mode" not in impl  # AGY_IMPLEMENT_MODE vazio = comportamento validado
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format")
    old = args_of(gem(make_settings()).run("x", context_key="c", run_id="r3", options={"skill": "review_implementation"}))
    assert "--mode" not in old


def test_gemini_implement_mode_configurable(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", HELP_REAL)
    a = gem(make_settings(AGY_IMPLEMENT_MODE="accept-edits"))
    impl = args_of(a.run("x", context_key="c", run_id="r", options={"skill": "implement_task"}))
    assert impl[impl.index("--mode") + 1] == "accept-edits"


def test_gemini_file_prompt_adds_prompts_dir(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", HELP_REAL)
    s = make_settings(AGY_PROMPT_MODE="file")
    a = args_of(gem(s).run("prompt", context_key="c", run_id="rf", options={"skill": "plan_feature"}))
    assert a[a.index("--add-dir") + 1] == str((s.runtime_dir / "prompts").resolve())


def test_gemini_quota_cooldown_blocks_next_calls(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "quota")
    s = make_settings()
    a = gem(s)
    with pytest.raises(QuotaExceededError):
        a.run("x", context_key="c", run_id="r")
    assert a.unavailable_reason()
    monkeypatch.setenv("FAKE_AGY_MODE", "success")
    with pytest.raises(QuotaExceededError):
        gem(s).run("x", context_key="c", run_id="r2")  # persistido em runtime/gemini_state.json
    assert gem(s).health().status == "quota_exceeded"
    a.clear_quota()
    assert gem(s).run("x", context_key="c", run_id="r3").text


def test_opencode_review_uses_plan_agent_in_separate_session(make_settings):
    fake = FakeOpenCode(agents=[{"name": "Build"}, {"name": "Plan"}])
    s = make_settings(OPENCODE_AGENT="Build")
    a = OpenCodeAdapter(s, session_store=Repository(s.db_path), transport=httpx.MockTransport(fake))
    a.run("impl", context_key="ctx", run_id="r1", options={"skill": "implement_task"})
    a.run("rev", context_key="ctx", run_id="r2", options={"skill": "review_implementation", "read_only": True})
    a.run("impl2", context_key="ctx", run_id="r3", options={"skill": "implement_task"})
    assert [b.get("agent") for b in fake.session_bodies] == ["Build", "Plan"]
    h = a.health()
    assert h.checks["review_agent"] == {"name": "Plan", "available": True}
