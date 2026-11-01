"""Fixtures comuns. TODOS os testes em tests/unit usam mocks/fakes — não provam integração real."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_settings  # noqa: E402

FAKE_AGY = ROOT / "tests" / "fakes" / "fake_agy.py"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "game"
    (ws / "src" / "shared").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "AGENTS.md").write_text("# AGENTS\nUse Luau estrito.\n", encoding="utf-8")
    (ws / "README.md").write_text("# AFK Evolution Simulator (teste)\n", encoding="utf-8")
    (ws / "docs" / "GDD.md").write_text("# GDD de teste\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=ws, check=True)
    return ws


@pytest.fixture
def make_settings(tmp_path: Path, workspace: Path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("OPENCODE_", "AGY_", "ORCH_", "MAX_", "FILE_LOCK", "A2A_", "CONTEXT_", "MCP_", "PROJECT_ROOT", "FINAL_REVIEW", "ALLOW_AGENT")):
            monkeypatch.delenv(key, raising=False)

    def _make(**overrides):
        base = {
            "PROJECT_ROOT": str(workspace),
            "ORCH_DB_PATH": str(tmp_path / "data" / "orchestrator.db"),
            "ORCH_RUNTIME_DIR": str(tmp_path / "runtime"),
            "ORCH_LOG_DIR": str(tmp_path / "logs"),
            "A2A_POLL_INTERVAL_MS": "50",
            "FILE_LOCK_WAIT_SECONDS": "0",
            "OPENCODE_SERVER_PASSWORD": "s3cr3t-test-pass",
            "MCP_CONFIG_FILE": str(tmp_path / "no_mcp.json"),
            "CHECKS_FILE": str(tmp_path / "no_checks.json"),  # testes n?o dependem do config/checks.json local
        }
        base.update({k: str(v) for k, v in overrides.items()})
        return load_settings(env_file=tmp_path / "absent.env", overrides=base)

    return _make


class AppHarness:
    def __init__(self, app):
        self.app = app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787", timeout=30)

    async def __aenter__(self):
        from api.main import startup
        await startup(self.app)
        return self

    async def __aexit__(self, *exc):
        from api.main import shutdown
        await self.client.aclose()
        await shutdown(self.app)

    async def wait_done(self, wf_id: str, timeout: float = 20.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d = (await self.client.get(f"/tasks/{wf_id}")).json()
            if d["status"] not in {"queued", "planning", "delegating", "reviewing"} and not d["running"]:
                return d
            await asyncio.sleep(0.05)
        raise AssertionError(f"workflow não terminou: {d['status']}")


@pytest.fixture
def harness(make_settings):
    from api.main import create_app

    def _make(backends, checks=None, **overrides):
        settings = make_settings(**overrides)
        app = create_app(settings, backends, a2a_in_process=True, configure_logging=False, checks=checks)
        return AppHarness(app)

    return _make
