"""[MOCK backends + A2A REAL] Papéis simétricos: quem implementa nunca revisa.

Modelos simulados; o protocolo A2A (Agent Cards, SendMessage, GetTask) é o do SDK oficial.
"""
import json
import sys
from pathlib import Path

from agents.mock_adapters import DEFAULT_TASK, build_mock_backends, plan_json, review_json, symmetric_responder
from orchestrator.checks import ChecksRunner, CheckSpec
from orchestrator.evidence import validate_finding
from orchestrator.schemas import ReviewFinding
from tests.conftest import ROOT, run

FAKE_CHECKER = str(ROOT / "tests" / "fakes" / "fake_checker.py")
EXAMPLE = "src/shared/Example.lua"


def writer(name: str, workspace: Path, contents=None, review_script=None, tasks=None, assignment="accept"):
    """Agente simulado que escreve no workspace de TESTE só quando implementa. `contents`: lista por tentativa."""
    base = symmetric_responder(name, review_script, files=[EXAMPLE], tasks=tasks, assignment=assignment)

    def respond(skill, prompt, calls):
        if skill == "implement_task":
            n = calls["count"]
            text = (contents[min(n - 1, len(contents) - 1)] if contents else f"return {{ version = {n} }}\n")
            (workspace / EXAMPLE).write_text(text, encoding="utf-8")
        return base(skill, prompt, calls)

    return respond


def task_for(agent: str, category: str = "other", **kw) -> dict:
    return {**DEFAULT_TASK, "assigned_to": agent, "category": category, **kw}


async def start(h, goal="Funcionalidade de teste"):
    r = await h.client.post("/tasks", json={"goal": goal})
    return await h.wait_done(r.json()["id"])


# ---------------------------------------------------------------- caminhos feliz e invertido
def test_deepseek_implements_gemini_reviews(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", ["changes_requested", "approved"], files=[])
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["status"] == "completed", d["error"]
            assert (st["assigned_to"], st["reviewer"], st["status"], st["review_cycles"]) == ("deepseek", "gemini", "approved", 1)
            assert len(b["deepseek"].skill_calls("implement_task")) == 2
            assert len(b["gemini"].skill_calls("review_implementation")) == 2
            assert b["deepseek"].skill_calls("review_implementation") == []  # nunca revisa o próprio trabalho
            assert all(c["read_only"] for c in b["gemini"].skill_calls("review_implementation"))
            assert b["deepseek"].skill_calls("respond_assignment")  # turno 2 da negociação
    run(go())


def test_gemini_implements_deepseek_reviews(harness, workspace):
    async def go():
        tasks = [task_for("gemini", "security", assignment_reason="valida RemoteEvent no servidor")]
        b = build_mock_backends()
        b["gemini"].responder = writer("gemini", workspace, tasks=tasks)
        b["deepseek"].responder = symmetric_responder("deepseek", ["changes_requested", "approved"], tasks=tasks)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["status"] == "completed", d["error"]
            assert (st["assigned_to"], st["reviewer"], st["status"]) == ("gemini", "deepseek", "approved")
            assert len(b["gemini"].skill_calls("implement_task")) == 2
            assert len(b["deepseek"].skill_calls("review_implementation")) == 2
            assert b["gemini"].skill_calls("review_implementation") == []
            review_prompt = b["deepseek"].skill_calls("review_implementation")[0]["prompt"]
            for item in ("acceptance_criteria", "roblox_api_misuse", "edge_case", "client_server_security", "EVIDÊNCIA OBRIGATÓRIA"):
                assert item in review_prompt
            assert "MODO SOMENTE LEITURA" in review_prompt
    run(go())


# ---------------------------------------------------------------- contraproposta e desempate
def test_counter_proposal_and_deterministic_tiebreak(harness, workspace):
    async def go():
        # Proposta: DeepSeek faz uma tarefa de segurança. DeepSeek contesta -> regra category:security -> Gemini.
        tasks = [task_for("deepseek", "security")]
        b = build_mock_backends()
        b["gemini"].responder = writer("gemini", workspace, tasks=tasks)
        b["deepseek"].responder = symmetric_responder("deepseek", ["approved"], tasks=tasks, assignment={"T1": "gemini"})
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert (st["assigned_to"], st["reviewer"]) == ("gemini", "deepseek")
            a = st["assignment"]
            assert (a["response"], a["decided_by"], a["rule"], a["proposed_to"]) == ("counter", "rules", "category:security", "deepseek")
            assert len(b["deepseek"].skill_calls("respond_assignment")) == 1  # sem terceira rodada
            events = [e["event"] for e in d["events"]]
            assert {"assignment_proposed", "assignment_response", "assignment_rules", "metrics"} <= set(events)
            m = d["metrics"]["assignment"]
            assert m["contested"] == 1 and m["contested_by"] == {"deepseek": 1} and m["tiebreak_winner"] == {"gemini": 1}
            assert m["outcome_changed_by_negotiation"] == 1
            assert d["metrics"]["calls_per_agent"]["deepseek"]["respond_assignment"] == 1
            msgs = (await h.client.get(f"/tasks/{d['id']}/messages")).json()
            assert any(x["direction"] == "decision" and x["skill"] == "assignment" for x in msgs)
    run(go())


# ---------------------------------------------------------------- Gemini sem cota
def test_gemini_without_quota_deepseek_plans_and_implements_review_waits(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].quota_reason = "Quota semanal do Argon excedida (simulada)"
        b["deepseek"].responder = writer("deepseek", workspace, tasks=[task_for("gemini", "server")])
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["planned_by"] == "deepseek" and d["planned_by_fallback"] is True
            assert (st["assigned_to"], st["reviewer"], st["status"]) == ("deepseek", "gemini", "awaiting_review")
            assert st["assignment"]["decided_by"] == "quota_fallback" and "quota" in st["assignment"]["reason"].lower()
            assert b["gemini"].calls == [] and b["deepseek"].skill_calls("review_implementation") == []
            assert d["status"] == "blocked"
            # Gemini volta: retomada vai direto para a revisão (não reimplementa) e valida o plano de fallback
            b["gemini"].quota_reason = None
            assert (await h.client.post(f"/tasks/{d['id']}/resume")).status_code == 202
            d2 = await h.wait_done(d["id"])
            assert d2["status"] == "completed", d2["error"]
            assert len(b["deepseek"].skill_calls("implement_task")) == 1
            rp = b["gemini"].skill_calls("review_implementation")[0]["prompt"]
            assert "VALIDAÇÃO DO PLANO" in rp
            assert d2["plan"]["plan_validated_by"] == "gemini"
            assert any(e["event"] == "fallback_plan_validated" for e in d2["events"])
    run(go())


def test_blocked_limit_pauses_workflow_and_dependents_wait_for_approval(harness, workspace):
    async def go():
        tasks = [task_for("deepseek", "ui", id=f"T{i}", title=f"Tarefa {i}", files=[f"src/shared/M{i}.lua"]) for i in (1, 2, 3)]
        tasks.append(task_for("deepseek", "ui", id="T4", title="Dependente", files=["src/shared/D.lua"], depends_on=["T1"]))
        b = build_mock_backends()
        b["gemini"].quota_reason = "sem cota (simulada)"
        b["deepseek"].responder = symmetric_responder("deepseek", tasks=tasks, files=[])
        async with harness(b, MAX_BLOCKED_SUBTASKS=3, MAX_PARALLEL_SUBTASKS=1) as h:
            d = await start(h)
            statuses = {s["plan_id"]: s["status"] for s in d["subtasks"]}
            assert d["status"] == "paused"
            assert [statuses[f"T{i}"] for i in (1, 2, 3)] == ["awaiting_review"] * 3
            assert statuses["T4"] == "pending"  # dependência só implementada (não aprovada) -> não inicia
            assert any(e["event"] == "paused_blocked_limit" for e in d["events"])
    run(go())


# ---------------------------------------------------------------- checagens automáticas
def test_failing_check_returns_to_implementer_without_llm_review(harness, workspace):
    async def go():
        checks = ChecksRunner(workspace, specs=[CheckSpec(id="fake", binary=sys.executable, args=[FAKE_CHECKER, "{files}"],
                                                          enabled=True)], allow_unlisted=True)
        b = build_mock_backends()
        b["deepseek"].responder = writer("deepseek", workspace, contents=["BAD\n", "return { ok = true }\n"])
        async with harness(b, checks=checks) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["status"] == "completed", d["error"]
            assert (st["check_fix_attempts"], st["impl_attempts"], st["review_cycles"]) == (1, 2, 0)
            assert len(b["gemini"].skill_calls("review_implementation")) == 1  # só depois que a checagem passou
            fix_prompt = b["deepseek"].skill_calls("implement_task")[1]["prompt"]
            assert "CHECAGENS AUTOMÁTICAS FALHARAM" in fix_prompt and "BAD encontrado" in fix_prompt
            assert '"status": "passed"' in b["gemini"].skill_calls("review_implementation")[0]["prompt"]
    run(go())


def test_identical_check_failure_escalates_early(harness, workspace):
    async def go():
        checks = ChecksRunner(workspace, specs=[CheckSpec(id="fake", binary=sys.executable, args=[FAKE_CHECKER, "{files}"],
                                                          enabled=True)], allow_unlisted=True)
        b = build_mock_backends()
        b["deepseek"].responder = writer("deepseek", workspace, contents=["BAD\n"])
        async with harness(b, checks=checks, MAX_CHECK_FIX_ATTEMPTS=5) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["status"] == "needs_human" and st["escalation"]["code"] == "checks_no_progress"
            assert st["impl_attempts"] == 2 and b["gemini"].skill_calls("review_implementation") == []
    run(go())


def test_no_active_checks_does_not_break_flow(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == "completed" and d["subtasks"][0]["checks"]["status"] == "no_active_checks"
            st = (await h.client.get("/checks/status")).json()
            assert st["any_active"] is False
    run(go())


# ---------------------------------------------------------------- escalonamento e retomada
def test_cycles_exhausted_go_to_needs_human_then_resume_with_decision(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", ["changes_requested"] * 3 + ["approved"], files=[])
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b, MAX_REVIEW_CYCLES=2) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["status"] == "needs_human" and st["status"] == "needs_human"
            esc = st["escalation"]
            assert esc["code"] == "review_cycles_exhausted" and esc["reviewer"]["agent"] == "gemini"
            assert esc["implementer"]["agent"] == "deepseek" and esc["reviewer"]["findings"]
            assert st["review_cycles"] == 2 and len(b["gemini"].skill_calls("review_implementation")) == 3
            # retomar sem decidir é recusado
            assert (await h.client.post(f"/tasks/{d['id']}/resume")).status_code == 409
            r = await h.client.post(f"/tasks/{d['id']}/subtasks/{st['id']}/decision",
                                    json={"action": "retry", "note": "Use o padrão do AGENTS.md"})
            assert r.status_code == 200
            assert (await h.client.post(f"/tasks/{d['id']}/resume")).status_code == 202
            d2 = await h.wait_done(d["id"])
            assert d2["status"] == "completed", d2["error"]
            assert "Use o padrão do AGENTS.md" in b["deepseek"].skill_calls("implement_task")[-1]["prompt"]
    run(go())


def test_human_reassign_swaps_roles(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = writer("gemini", workspace, review_script=["changes_requested"] * 5)
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b, MAX_REVIEW_CYCLES=0) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["status"] == "needs_human"
            await h.client.post(f"/tasks/{d['id']}/subtasks/{st['id']}/decision", json={"action": "reassign", "note": "troca"})
            await h.client.post(f"/tasks/{d['id']}/resume")
            d2 = await h.wait_done(d["id"])
            st2 = d2["subtasks"][0]
            assert (st2["assigned_to"], st2["reviewer"], st2["status"]) == ("gemini", "deepseek", "approved")
            assert st2["assignment"]["decided_by"] == "human"
    run(go())


# ---------------------------------------------------------------- A: reformulação única da revisão
def test_all_findings_invalid_one_reformulation_then_success(harness, workspace):
    async def go():
        bad = {"file": EXAMPLE, "line": 1, "snippet": "trecho que não existe", "issue": "x"}
        b = build_mock_backends()
        replies = iter([review_json("changes_requested", bad), review_json("approved")])
        b["gemini"].responder = lambda skill, prompt, calls: next(replies) if skill == "review_implementation" \
            else symmetric_responder("gemini", files=[])(skill, prompt, calls)
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert d["status"] == "completed" and st["status"] == "approved"
            calls = b["gemini"].skill_calls("review_implementation")
            assert len(calls) == 2 and "REFORMULAÇÃO OBRIGATÓRIA" in calls[1]["prompt"]
            assert len(b["deepseek"].skill_calls("implement_task")) == 1  # reformulação não gasta ciclo de ajuste
            events = [e["event"] for e in d["events"]]
            assert "findings_discarded" in events and "review_reformulation_requested" in events
    run(go())


def test_findings_still_invalid_after_reformulation_goes_to_needs_human(harness, workspace):
    async def go():
        bad = {"file": EXAMPLE, "line": 99, "snippet": "nada", "issue": "x"}
        b = build_mock_backends()
        b["gemini"].responder = lambda skill, prompt, calls: review_json("changes_requested", bad) if skill == "review_implementation" \
            else symmetric_responder("gemini", files=[])(skill, prompt, calls)
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["status"] == "needs_human" and st["escalation"]["code"] == "review_unverified"
            assert len(b["gemini"].skill_calls("review_implementation")) == 2  # original + 1 reformulação
            assert any(e["event"] == "review_unverified" for e in d["events"])
            assert st["review"] is None or st["review"].get("verdict") != "approved"  # não converte em aprovado
    run(go())


# ---------------------------------------------------------------- B: evidência com snippet (±5 linhas)
def test_snippet_validation_window_and_workspace_lock(workspace):
    f = workspace / "src" / "shared" / "Mod.lua"
    f.write_text("\n".join(f"local v{i} = {i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    ok = lambda **kw: validate_finding(workspace, ReviewFinding(file="src/shared/Mod.lua", **kw))  # noqa: E731
    assert ok(line=10, snippet="local v10 = 10")[0]
    assert ok(line=10, snippet="local   v15 =   15")[0]          # +5 linhas, espaços normalizados
    assert not ok(line=10, snippet="local v16 = 16")[0]           # +6 linhas: fora da tolerância
    assert ok(line=10, snippet="local v5 = 5")[0]                 # -5 linhas
    assert not ok(line=10, snippet="")[0]
    assert not ok(line=50, snippet="local v1 = 1")[0]             # linha inexistente
    assert not validate_finding(workspace, ReviewFinding(file="../fora.lua", line=1, snippet="x"))[0]
    assert not validate_finding(workspace, ReviewFinding(file="src/nao_existe.lua", line=1, snippet="x"))[0]


# ---------------------------------------------------------------- revisor alterou arquivos durante a revisão
def test_review_invalidated_when_reviewer_changes_files(harness, workspace):
    async def go():
        b = build_mock_backends()
        base = symmetric_responder("gemini", ["approved"], files=[])

        def sneaky(skill, prompt, calls):
            if skill == "review_implementation":
                (workspace / EXAMPLE).write_text("-- alterado pelo revisor\n", encoding="utf-8")
            return base(skill, prompt, calls)

        b["gemini"].responder = sneaky
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["status"] == "needs_human" and st["escalation"]["code"] == "reviewer_modified_files"
            assert any(e["event"] == "review_invalidated_files_changed" for e in d["events"])
    run(go())


def test_implementation_attempt_ceiling(harness, workspace):
    async def go():
        checks = ChecksRunner(workspace, specs=[CheckSpec(id="fake", binary=sys.executable, args=[FAKE_CHECKER, "{files}"],
                                                          enabled=True)], allow_unlisted=True)
        b = build_mock_backends()
        # cada tentativa falha com saída diferente (sem "sem progresso"), até o teto global
        b["deepseek"].responder = writer("deepseek", workspace, contents=[f"BAD {i}\n" for i in range(10)])
        async with harness(b, checks=checks, MAX_CHECK_FIX_ATTEMPTS=10, MAX_IMPLEMENTATION_ATTEMPTS=3) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["escalation"]["code"] == "implementation_attempts_exhausted" and st["impl_attempts"] == 3
    run(go())


def test_negotiation_can_be_disabled_to_save_quota(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b, ASSIGNMENT_NEGOTIATION="false") as h:
            d = await start(h)
            assert d["status"] == "completed"
            assert b["deepseek"].skill_calls("respond_assignment") == []
            assert d["subtasks"][0]["assignment"]["decided_by"] == "no_negotiation"
    run(go())


def test_coordinator_refuses_self_review_even_if_state_is_corrupted(harness, workspace):
    """Mesmo com o banco adulterado (assigned_to trocado depois da implementação), o autor nunca revisa."""
    async def go():
        b = build_mock_backends()
        b["gemini"].quota_reason = "sem cota (simulada)"
        b["deepseek"].responder = writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            st = d["subtasks"][0]
            assert st["status"] == "awaiting_review" and st["result"]["implemented_by"] == "deepseek"
            repo = h.app.state.container.repo
            repo.update_subtask(st["id"], assigned_to="gemini", reviewer="deepseek")  # corrupção simulada
            b["gemini"].quota_reason = None
            await h.client.post(f"/tasks/{d['id']}/resume")
            d2 = await h.wait_done(d["id"])
            assert b["deepseek"].skill_calls("review_implementation") == []
            assert len(b["gemini"].skill_calls("review_implementation")) == 1
            assert d2["subtasks"][0]["status"] == "approved"
    run(go())
