"""FINAL_REVIEW=false grava `disabled` (API) e o painel exibe o selo "revisão final desligada".

O selo é calculado por uma função JS pura extraída do dashboard e executada com Node (pulado se Node não estiver instalado).
"""
import json
import re
import shutil
import subprocess

import pytest

from agents.mock_adapters import build_mock_backends
from orchestrator.task_manager import FR, WF
from tests.conftest import ROOT, run
from tests.unit.test_final_review import impl_writer, start

NODE = shutil.which("node")


def badge(workflow: dict) -> str:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    fn = re.search(r"/\*FR-BADGE-START\*/(.*?)/\*FR-BADGE-END\*/", html, re.S).group(1)
    esc = re.search(r"const esc = .*?;\n", html).group(0)
    script = esc + fn + f"\nprocess.stdout.write(finalReviewBadge({json.dumps(workflow)}));"
    return subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20).stdout


def test_final_review_disabled_is_recorded_and_status_completed(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b, FINAL_REVIEW="false") as h:
            d = await start(h)
            assert d["status"] == WF.COMPLETED and d["final_review_state"] == FR.DISABLED
            assert b["gemini"].skill_calls("final_review") == [] and b["deepseek"].skill_calls("final_review") == []
            assert any(e["event"] == "final_review_disabled" for e in d["events"])
            listed = next(w for w in (await h.client.get("/tasks")).json() if w["id"] == d["id"])
            assert listed["final_review_state"] == FR.DISABLED  # a lista do painel recebe o estado
    run(go())


@pytest.mark.skipif(not NODE, reason="Node.js não instalado; selo verificado só pela API")
def test_dashboard_shows_disabled_seal_and_unreviewed_warning():
    assert "revisão final desligada" in badge({"status": "completed", "final_review_state": "disabled"})
    assert "sem revisão final" in badge({"status": "completed_unreviewed", "final_review_state": "skipped"})
    assert "aceita por decisão humana" in badge({"status": "completed", "final_review_state": "accepted_by_human"})
    assert badge({"status": "completed", "final_review_state": "passed"}) == ""


def test_dashboard_contains_context_menu_and_cancel_elements():
    """Garante a presença dos elementos de menu de contexto e botões de interrupção na interface."""
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert 'id="chatContextMenu"' in html
    assert "cmi-danger" in html
    assert "Interromper Geração" in html
    assert 'id="cancelGenBtn"' in html
    assert "openChatMenu" in html
    assert "handleChatMenuAction" in html

