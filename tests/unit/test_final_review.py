"""[MOCK backends + A2A REAL] Revisão final: nunca silenciosa, nunca autorrevisão, mesma exigência de evidência."""
from pathlib import Path

from agents.mock_adapters import DEFAULT_FINDING, DEFAULT_TASK, build_mock_backends, review_json, symmetric_responder
from orchestrator.task_manager import FR, WF
from tests.conftest import run

EXAMPLE = "src/shared/Example.lua"
OTHER = "src/shared/Other.lua"


def impl_writer(name, workspace: Path, rel=EXAMPLE, tasks=None, review_script=None, final=None):
    """Agente simulado: escreve `rel` ao implementar; `final` controla as respostas de final_review (lista ou callable)."""
    base = symmetric_responder(name, review_script, files=[rel], tasks=tasks)
    finals = list(final) if isinstance(final, list) else None

    def respond(skill, prompt, calls):
        if skill == "implement_task":
            (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
            (workspace / rel).write_text(f"return {{ version = {calls['count']} }}\n", encoding="utf-8")
        if skill == "final_review":
            if callable(final):
                return final(prompt, calls)
            if finals:
                return finals[min(calls["count"] - 1, len(finals) - 1)]
        return base(skill, prompt, calls)

    return respond


async def start(h, goal="Funcionalidade de teste"):
    r = await h.client.post("/tasks", json={"goal": goal})
    return await h.wait_done(r.json()["id"])


def test_final_reviewer_without_quota_gives_completed_unreviewed_not_completed(harness, workspace):
    async def go():
        b = build_mock_backends()
        base = symmetric_responder("gemini", ["approved"], files=[])

        def gemini(skill, prompt, calls):
            out = base(skill, prompt, calls)
            if skill == "review_implementation":
                b["gemini"].quota_reason = "cota do Argon esgotada após a revisão da subtarefa (simulada)"
            return out

        b["gemini"].responder = gemini
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["subtasks"][0]["status"] == "approved"
            assert d["status"] == WF.COMPLETED_UNREVIEWED and d["final_review_state"] == FR.SKIPPED
            assert d["final_review"]["reviewer"] == "gemini" and d["final_review"]["attempt"] == 1
            assert b["gemini"].skill_calls("final_review") == []
            assert b["deepseek"].skill_calls("final_review") == []  # nunca o autor revisa o próprio trabalho
            assert "Retomar" in d["error"]
            assert any(e["event"] == "final_review_skipped" for e in d["events"])
    run(go())


def test_negative_final_verdict_goes_to_needs_human_with_opinion_and_no_auto_fix(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, final=[review_json("changes_requested", DEFAULT_FINDING)])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN and d["final_review_state"] == FR.CHANGES_REQUESTED
            esc = d["final_review"]["escalation"]
            assert esc["reviewer"] == "gemini" and esc["findings"] and esc["findings"][0]["file"] == EXAMPLE
            assert "revisão simulada: changes_requested" in d["error"]
            assert len(b["deepseek"].skill_calls("implement_task")) == 1  # sem ciclo automático de correção
            assert d["subtasks"][0]["status"] == "approved"  # subtarefa não é reaberta
    run(go())


def test_rejected_final_verdict_also_needs_human(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, final=[review_json("rejected", DEFAULT_FINDING)])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN and d["final_review_state"] == FR.REJECTED
    run(go())


def test_gemini_implemented_everything_so_deepseek_does_final_review(harness, workspace):
    async def go():
        tasks = [{**DEFAULT_TASK, "assigned_to": "gemini", "category": "security"}]
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, tasks=tasks)
        b["deepseek"].responder = symmetric_responder("deepseek", ["approved"], tasks=tasks)
        async with harness(b) as h:
            d = await start(h)
            assert d["subtasks"][0]["assigned_to"] == "gemini"
            assert d["status"] == WF.COMPLETED and d["final_review"]["reviewer"] == "deepseek"
            assert len(b["deepseek"].skill_calls("final_review")) == 1
            assert b["gemini"].skill_calls("final_review") == []
            assert b["deepseek"].skill_calls("final_review")[0]["read_only"] is True
    run(go())


def _mixed(workspace, gemini_final=None):
    tasks = [{**DEFAULT_TASK, "id": "T1", "assigned_to": "deepseek", "files": [EXAMPLE]},
             {**DEFAULT_TASK, "id": "T2", "title": "Remote de compra", "assigned_to": "gemini", "category": "remotes", "files": [OTHER]}]
    b = build_mock_backends()
    b["gemini"].responder = impl_writer("gemini", workspace, rel=OTHER, tasks=tasks, final=gemini_final)
    b["deepseek"].responder = impl_writer("deepseek", workspace, rel=EXAMPLE, tasks=tasks)
    return b


def test_mixed_case_lists_self_authored_and_reviewer_approval_does_not_cover_them(harness, workspace):
    async def go():
        b = _mixed(workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.COMPLETED, d["error"]
            fr = d["final_review"]
            assert fr["reviewer"] == "gemini" and fr["self_authored_subtasks"] == ["T2"]
            cov = {c["id"]: c for c in fr["coverage"]}
            assert cov["T1"]["covered_by_final_review"] is True
            assert cov["T2"]["covered_by_final_review"] is False and cov["T2"]["coverage"] == "cross_review_only"
            assert cov["T2"]["approved_by"] == "deepseek"
            assert d["metrics"]["final_review"]["self_authored_subtasks"] == ["T2"]
            prompt = b["gemini"].skill_calls("final_review")[0]["prompt"]
            assert "você mesmo implementou" in prompt and '"T2"' in prompt
            for focus in ("interfaces", "remotes", "requires", "estado compartilhado", "nomes"):
                assert focus in prompt
            assert "NÃO re-revise cada subtarefa" in prompt
    run(go())


def test_mixed_case_findings_on_own_subtask_still_block(harness, workspace):
    async def go():
        finding = {"file": OTHER, "line": 1, "snippet": "return {", "issue": "remote sem validação", "severity": "blocker"}
        b = _mixed(workspace, gemini_final=[review_json("changes_requested", finding)])
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN and d["final_review_state"] == FR.CHANGES_REQUESTED
            assert d["final_review"]["escalation"]["findings"][0]["file"] == OTHER
    run(go())


def test_final_findings_without_evidence_get_one_reformulation_then_unverified(harness, workspace):
    async def go():
        bad = {"file": EXAMPLE, "line": 1, "snippet": "não existe no arquivo", "issue": "x"}
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, final=[review_json("changes_requested", bad)])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            calls = b["gemini"].skill_calls("final_review")
            assert len(calls) == 2 and "REFORMULAÇÃO OBRIGATÓRIA" in calls[1]["prompt"]
            assert d["status"] == WF.NEEDS_HUMAN and d["final_review_state"] == FR.UNVERIFIED
            assert d["final_review"]["discarded_findings"]
            assert any(e["event"] == "review_unverified" for e in d["events"])
    run(go())


def test_final_findings_reformulated_into_approval(harness, workspace):
    async def go():
        bad = {"file": EXAMPLE, "line": 40, "snippet": "x", "issue": "x"}
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace,
                                            final=[review_json("changes_requested", bad), review_json("approved")])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert len(b["gemini"].skill_calls("final_review")) == 2
            assert d["status"] == WF.COMPLETED and d["final_review_state"] == FR.PASSED
    run(go())


def test_final_review_invalidated_if_reviewer_changes_files(harness, workspace):
    async def go():
        def sneaky(prompt, calls):
            (workspace / EXAMPLE).write_text("-- mexido na revisão final\n", encoding="utf-8")
            return review_json("approved")

        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, final=sneaky)
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN and d["final_review_state"] == FR.INVALIDATED
            assert any(e["event"] == "review_invalidated_files_changed" for e in d["events"])
    run(go())


def test_final_review_records_snapshot_and_attempt(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            fr = d["final_review"]
            assert fr["attempt"] == 1 and len(fr["snapshot_hash"]) == 16 and fr["watched_files"] == [EXAMPLE]
    run(go())


# ---------------------------------------------------------------- retomada só da revisão final
SUB_FIELDS = ("assigned_to", "reviewer", "agent", "assignment_reason", "assignment", "review_cycles", "check_fix_attempts",
              "impl_attempts", "status", "result", "review", "a2a_context_id", "last_a2a_task_id")


def _unreviewed_setup(workspace):
    """DeepSeek implementa com 1 ciclo de ajuste; Gemini aprova a subtarefa e fica sem cota antes da revisão final."""
    b = build_mock_backends()
    base = symmetric_responder("gemini", ["changes_requested", "approved"], files=[])

    def gemini(skill, prompt, calls):
        out = base(skill, prompt, calls)
        if skill == "review_implementation" and calls["count"] == 2:
            b["gemini"].quota_reason = "sem cota (simulada)"
        return out

    b["gemini"].responder = gemini
    b["deepseek"].responder = impl_writer("deepseek", workspace)
    return b


def _skill_counts(b):
    return {(n, c["skill"]) for n in b for c in b[n].calls}, {n: len(b[n].calls) for n in b}


def test_resume_completed_unreviewed_runs_only_final_review(harness, workspace):
    async def go():
        b = _unreviewed_setup(workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.COMPLETED_UNREVIEWED
            before_sub = {k: d["subtasks"][0][k] for k in SUB_FIELDS}
            assert before_sub["review_cycles"] == 1
            calls_before = {n: [c["skill"] for c in b[n].calls] for n in b}
            n_events = len(d["events"])
            b["gemini"].quota_reason = None
            assert (await h.client.post(f"/tasks/{d['id']}/resume")).status_code == 202
            d2 = await h.wait_done(d["id"])
            assert d2["status"] == WF.COMPLETED and d2["final_review_state"] == FR.PASSED
            # só a revisão final foi chamada
            assert {n: [c["skill"] for c in b[n].calls] for n in b} == {
                "gemini": calls_before["gemini"] + ["final_review"], "deepseek": calls_before["deepseek"]}
            # atribuições e contadores intactos
            assert {k: d2["subtasks"][0][k] for k in SUB_FIELDS} == before_sub
            new_events = [e["event"] for e in d2["events"][n_events:]]
            assert "resume_final_review_only" in new_events
            assert "resume" not in new_events and not any(e["content"] == "delegating" for e in d2["events"][n_events:])
            assert d2["final_review"]["attempt"] == 2
            assert "final_review_workspace_changed" not in new_events  # nada mudou entre as tentativas
    run(go())


def test_resume_final_review_after_workspace_changed_runs_on_current_state(harness, workspace):
    async def go():
        b = _unreviewed_setup(workspace)
        async with harness(b) as h:
            d = await start(h)
            snap1 = d["final_review"]["snapshot_hash"]
            (workspace / EXAMPLE).write_text("return { version = 99 } -- editado à mão\n", encoding="utf-8")
            b["gemini"].quota_reason = None
            await h.client.post(f"/tasks/{d['id']}/resume")
            d2 = await h.wait_done(d["id"])
            fr = d2["final_review"]
            assert fr["snapshot_hash"] != snap1 and fr["workspace_changed_since_last_attempt"] is True
            assert any(e["event"] == "final_review_workspace_changed" for e in d2["events"])
            assert d2["status"] == WF.COMPLETED
    run(go())


def test_final_needs_human_accept_and_reject_decisions(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace, final=[review_json("changes_requested", DEFAULT_FINDING)])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN
            r = await h.client.post(f"/tasks/{d['id']}/final-review/decision", json={"action": "accept", "note": "ok assim"})
            assert r.status_code == 200 and r.json()["status"] == WF.COMPLETED
            d2 = (await h.client.get(f"/tasks/{d['id']}")).json()
            assert d2["final_review_state"] == FR.ACCEPTED_BY_HUMAN and d2["final_review"]["human_decision"]["previous_state"] == FR.CHANGES_REQUESTED
            # segunda execução: rejeição confirmada
            d3 = await start(h, "Outra funcionalidade")
            r = await h.client.post(f"/tasks/{d3['id']}/final-review/decision", json={"action": "reject", "note": "não"})
            assert r.json()["status"] == WF.FAILED
            # decisão fora de hora é recusada
            assert (await h.client.post(f"/tasks/{d['id']}/final-review/decision", json={"action": "reject"})).status_code == 409
    run(go())


def test_final_needs_human_resume_reruns_only_final_review(harness, workspace):
    async def go():
        b = build_mock_backends()
        b["gemini"].responder = impl_writer("gemini", workspace,
                                            final=[review_json("changes_requested", DEFAULT_FINDING), review_json("approved")])
        b["deepseek"].responder = impl_writer("deepseek", workspace)
        async with harness(b) as h:
            d = await start(h)
            assert d["status"] == WF.NEEDS_HUMAN
            await h.client.post(f"/tasks/{d['id']}/resume")
            d2 = await h.wait_done(d["id"])
            assert d2["status"] == WF.COMPLETED and d2["final_review"]["attempt"] == 2
            assert len(b["deepseek"].skill_calls("implement_task")) == 1
    run(go())
