"""[MOCK] Runner de checagens: lista fixa, sem shell, skipped quando ausente, falha com assinatura estável."""
import sys

from orchestrator.checks import ChecksRunner, CheckSpec
from tests.conftest import ROOT

FAKE = str(ROOT / "tests" / "fakes" / "fake_checker.py")


def fake_spec(**kw):
    # O "binário" é o python; o script falso entra como primeiro argumento.
    return CheckSpec(id="fake", binary=sys.executable, args=[FAKE, "{files}"], enabled=True, **kw)


def test_default_config_has_everything_disabled(workspace):
    r = ChecksRunner(workspace, ROOT / "config" / "checks.json")
    st = r.status()
    assert st["checks"] and not st["any_active"]
    out = r.run(["src/shared/A.lua"])
    assert out["status"] == "no_active_checks"
    assert all(x["status"] == "skipped" for x in out["results"])


def test_missing_tool_is_skipped_not_error(workspace, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"checks": [{"id": "selene", "binary": "selene-nao-instalado", "enabled": true}]}')
    out = ChecksRunner(workspace, cfg).run(["x.lua"])
    assert out["status"] == "no_active_checks" and "fora da lista" in out["results"][0]["reason"]
    cfg.write_text('{"checks": [{"id": "selene", "binary": "selene", "enabled": true}]}')
    out = ChecksRunner(workspace, cfg).run(["x.lua"])
    assert out["results"][0]["status"] == "skipped"


def test_unlisted_tool_refused_without_test_flag(workspace):
    out = ChecksRunner(workspace, specs=[fake_spec()]).run(["src/a.lua"])
    assert out["results"][0]["reason"] == "ferramenta fora da lista permitida"


def test_fake_checker_fails_then_passes_with_stable_signature(workspace):
    f = workspace / "src" / "shared" / "A.lua"
    f.write_text("BAD\n")
    r = ChecksRunner(workspace, specs=[fake_spec()], allow_unlisted=True)
    first, second = r.run(["src/shared/A.lua"]), r.run(["src/shared/A.lua"])
    assert first["status"] == "failed" and first["signature"] == second["signature"]
    f.write_text("return {}\n")
    assert r.run(["src/shared/A.lua"])["status"] == "passed"


def test_paths_outside_workspace_never_passed_to_tool(workspace):
    r = ChecksRunner(workspace, specs=[fake_spec()], allow_unlisted=True)
    out = r.run(["../fora.lua", "/etc/passwd"])
    assert out["results"][0]["status"] == "skipped"
