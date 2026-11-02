"""[MOCK] Correções 0.4.2: null nos contratos, caminho original nas checagens, sintaxe do painel."""
import re
import shutil
import subprocess
import sys

import pytest

from core.safe_exec import CommandResult
from orchestrator.checks import CheckSpec, ChecksRunner
from orchestrator.schemas import ImplementationReport, Plan, ReviewResult
from tests.conftest import ROOT


def test_null_fields_use_defaults():
    r = ReviewResult.model_validate({"verdict": "approved", "summary": "ok", "rejection_reason": None, "plan_valid": None,
                                     "findings": [], "decisions": [{"title": "t", "decision": "d", "rationale": None}]})
    assert r.structured and r.verdict == "approved" and r.rejection_reason == "" and r.decisions[0].rationale == ""
    rep = ImplementationReport.model_validate({"status": "done", "summary": None, "result": None, "issues": None})
    assert rep.structured and rep.summary == "" and rep.result == "" and rep.issues == []
    p = Plan.model_validate({"summary": None, "tasks": [{"title": "x", "context": None, "files": None}]})
    assert p.summary == "" and p.tasks[0].context == "" and p.tasks[0].files == []


def test_checks_keep_original_case(workspace):
    (workspace / "src" / "shared" / "BigNum.luau").write_text("return {}\n", encoding="utf-8")
    seen = []

    def runner(cmd, cwd, timeout):
        seen.append(cmd)
        return CommandResult(0, "", "")

    r = ChecksRunner(workspace, specs=[CheckSpec(id="fake", binary=sys.executable, args=["{files}"],
                                                 file_globs=["*.luau"], enabled=True)],
                     allow_unlisted=True, runner=runner)
    out = r.run(["src/shared/BigNum.luau", "./src/shared/BigNum.luau"])
    assert out["status"] == "passed" and seen[0][1:] == ["src/shared/BigNum.luau"]
    assert out["results"][0]["files"] == ["src/shared/BigNum.luau"]


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js não instalado")
def test_dashboard_js_syntax_and_scroll_guard(tmp_path):
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    f = tmp_path / "dash.js"
    f.write_text(js, encoding="utf-8")
    subprocess.run([shutil.which("node"), "--check", str(f)], check=True, capture_output=True, timeout=20)
    assert "function setHTML" in js and "loadDetail(true)" in js
    assert "setHTML($('#gBody')" in js or "setHTML($('#tbody')" in js
