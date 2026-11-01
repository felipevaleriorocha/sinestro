"""[MOCK] Leitura de configuração, redação de segredos e execução controlada."""
from pathlib import Path

import pytest

from core.config import load_settings
from core.logging_setup import redact, register_secrets
from core.safe_exec import CommandNotAllowed, run_git


def test_reads_env_file_without_modifying_it(tmp_path: Path, monkeypatch):
    for k in ("PROJECT_ROOT", "OPENCODE_MODEL", "OPENCODE_SERVER_PASSWORD", "AGY_MODEL", "OPENCODE_AGENT", "MAX_REVIEW_CYCLES"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    content = (
        'PROJECT_ROOT="C:\\Users\\Felipe\\.gemini\\antigravity\\scratch\\afk-evolution-simulator"\n'
        "OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free\n"
        "OPENCODE_SERVER_PASSWORD=abc=123\\x\n"
        "OPENCODE_AGENT=build\n"
        "AGY_MODEL=gemini-4-argon-high\n"
        "MAX_REVIEW_CYCLES=3\n"
    )
    env.write_text(content, encoding="utf-8")
    s = load_settings(env_file=env)
    assert env.read_text(encoding="utf-8") == content
    assert "afk-evolution-simulator" in str(s.project_root)
    assert "\\antigravity" in str(s.project_root)  # sem interpretar \a como escape
    assert s.opencode_model == "opencode/deepseek-v4.1-flash-free"
    assert s.opencode_password == "abc=123\\x"
    assert s.opencode_agent == "build"
    assert s.max_review_cycles == 3
    assert s.agy_model == "gemini-4-argon-high"


def test_defaults_and_model_switch_via_env(tmp_path, monkeypatch):
    for k in ("OPENCODE_MODEL", "OPENCODE_AGENT", "ORCH_HOST", "ORCH_PORT", "ORCH_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(env_file=tmp_path / "x.env", overrides={"PROJECT_ROOT": str(tmp_path)})
    assert s.agy_model == "gemini-4-argon-high"
    assert s.opencode_model == "opencode/deepseek-v4.1-flash-free"
    assert s.opencode_agent == ""
    assert s.public_url == "http://127.0.0.1:8787"
    s2 = load_settings(env_file=tmp_path / "x.env", overrides={"PROJECT_ROOT": str(tmp_path), "OPENCODE_MODEL": "opencode/outro-modelo"})
    assert s2.opencode_model == "opencode/outro-modelo"


def test_public_summary_never_contains_password(make_settings):
    s = make_settings()
    assert "s3cr3t-test-pass" not in str(s.public_summary())
    assert "s3cr3t-test-pass" not in repr(s)


def test_redaction():
    register_secrets(["minha-senha-123"])
    assert "minha-senha-123" not in redact("erro com minha-senha-123 no texto")
    assert "dXNlcjpwYXNz" not in redact("Authorization: Basic dXNlcjpwYXNz")
    assert "pw" not in redact("http://user:pw@host/x").split("@")[0].split("//")[1]
    assert "OPENCODE_SERVER_PASSWORD=***" in redact("OPENCODE_SERVER_PASSWORD=abc")


def test_git_only_allows_read_only_subcommands(tmp_path):
    for bad in (["push"], ["reset", "--hard"], ["checkout", "."], ["commit", "-m", "x"], ["clean", "-fd"]):
        with pytest.raises(CommandNotAllowed):
            run_git(bad, tmp_path)
