"""Coordenador do fluxo multiagente com papéis simétricos (todas as interações entre agentes passam por A2A).

Regra central: quem implementa NUNCA revisa o próprio trabalho (reviewer = o outro agente).

Por workflow:
  1. Planejamento: Gemini (ou DeepSeek se o Gemini estiver sem cota -> plano marcado `planned_by_fallback`).
  2. Atribuição: proposta no plano (turno 1) -> o outro agente aceita/contesta tudo em UMA chamada (turno 2)
     -> desempate determinístico por config/assignment_rules.json (sem 3ª rodada).
Por subtarefa (em ordem; dependentes só começam quando a dependência está `approved`):
  3. Implementação (assigned_to)
  4. Checagens automáticas (falha => volta ao implementador sem gastar revisão de LLM)
  5. Revisão cruzada somente leitura (reviewer), com achados exigindo evidência (arquivo, linha, trecho)
  6. Ajustes limitados; discordância persistente => `needs_human` (sem nova rodada de debate)
Limites: MAX_REVIEW_CYCLES, MAX_CHECK_FIX_ATTEMPTS, MAX_IMPLEMENTATION_ATTEMPTS e MAX_BLOCKED_SUBTASKS (pausa).
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from communication.a2a_client import A2AAgentClient, A2ADelegationError
from communication.messaging import DelegationResult
from core.config import Settings
from core.logging_setup import redact
from database.repository import Repository
from mcp_bridge.server_registry import MCPServerRegistry
from memory.context_manager import ContextManager
from memory.decision_store import DecisionStore
from orchestrator.agent_registry import AgentRegistry
from orchestrator.assignment import AssignmentRules, other, resolve_assignment
from orchestrator.checks import ChecksRunner
from orchestrator.evidence import review_problem, validate_review
from orchestrator.file_locks import FileLockManager, LockConflictError, PathOutsideWorkspaceError, normalize_file
from orchestrator.git_integration import GitInspector, _sha
from orchestrator.schemas import (
    AssignmentDecision, AssignmentResponse, ImplementationReport, Plan, ReviewResult,
)
from orchestrator.task_manager import ST, WF, TaskManager

log = logging.getLogger("orchestrator.coordinator")

UNAVAILABLE_CODES = {"quota_exceeded", "agent_unavailable", "connection_failed", "model_unavailable", "contract_not_verified"}


class WorkflowAbort(RuntimeError):
    def __init__(self, message: str, status: str = WF.FAILED):
        super().__init__(message)
        self.status = status


class SubtaskStop(Exception):
    """Interrompe o ciclo da subtarefa (status já gravado)."""


class Coordinator:
    def __init__(self, settings: Settings, repo: Repository, tasks: TaskManager, registry: AgentRegistry,
                 a2a: A2AAgentClient, context: ContextManager, decisions: DecisionStore, locks: FileLockManager,
                 git: GitInspector, mcp: MCPServerRegistry, checks: ChecksRunner | None = None,
                 rules: AssignmentRules | None = None):
        self.s = settings
        self.repo = repo
        self.tasks = tasks
        self.registry = registry
        self.a2a = a2a
        self.context = context
        self.decisions = decisions
        self.locks = locks
        self.git = git
        self.mcp = mcp
        self.checks = checks or ChecksRunner(settings.project_root, settings.checks_file)
        self.rules = rules or AssignmentRules.load(settings.assignment_rules_file)
        self._running: dict[str, asyncio.Task] = {}
        self._workflow_gate = asyncio.Semaphore(1)  # workflows serializados (atribuição de mudanças via Git fica confiável)

    # ================================================================ API pública
    def start(self, wf_id: str, resume: bool = False) -> None:
        if wf_id in self._running and not self._running[wf_id].done():
            raise RuntimeError("Workflow já está em execução.")
        self._running[wf_id] = asyncio.create_task(self._run_guarded(wf_id, resume), name=f"wf-{wf_id}")

    def is_running(self, wf_id: str) -> bool:
        t = self._running.get(wf_id)
        return bool(t and not t.done())

    async def cancel(self, wf_id: str) -> None:
        self.tasks.request_cancel(wf_id)
        wf = self.repo.get_workflow(wf_id)
        if wf and wf["status"] == WF.QUEUED and not self.is_running(wf_id):
            self.tasks.set_status(wf_id, WF.CANCELED)

    async def shutdown(self) -> None:
        for t in self._running.values():
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)

    # ================================================================ workflow
    async def _run_guarded(self, wf_id: str, resume: bool) -> None:
        async with self._workflow_gate:
            try:
                await self.run_workflow(wf_id, resume)
            except asyncio.CancelledError:
                self.tasks.set_status(wf_id, WF.INTERRUPTED, error="Execução interrompida (serviço encerrando).")
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Workflow %s falhou", wf_id)
                self.tasks.set_status(wf_id, WF.FAILED, error=redact(f"{type(exc).__name__}: {exc}"))
                self.tasks.event(wf_id, "orchestrator", "error", redact(str(exc)), "error")
            finally:
                self.locks.release_workflow(wf_id)
                self.context.forget(wf_id)

    def _should_cancel(self, wf_id: str):
        return lambda: self.tasks.cancel_requested(wf_id)

    async def run_workflow(self, wf_id: str, resume: bool = False) -> str:
        wf = self.repo.get_workflow(wf_id)
        if not wf:
            raise RuntimeError("Workflow inexistente.")
        goal, mode = wf["goal"], wf.get("mode") or "full"
        if not self.s.project_root.is_dir():
            self.tasks.set_status(wf_id, WF.FAILED, error="PROJECT_ROOT não existe. Configure o .env.")
            return WF.FAILED

        bundle = await asyncio.to_thread(self.context.for_workflow, wf_id)
        self.repo.update_workflow(wf_id, context_hash=bundle.hash, context_sources_json=bundle.to_dict())
        if bundle.missing:
            self.tasks.event(wf_id, "orchestrator", "context_missing", f"Documentos não encontrados: {', '.join(bundle.missing)}", "warning")
        mcp_status = await asyncio.to_thread(self.mcp.status)

        try:
            if resume and wf.get("plan") and self.tasks.subtasks(wf_id):
                plan = wf["plan"]
                reset = self.tasks.reset_for_resume(wf_id)
                self.tasks.event(wf_id, "orchestrator", "resume", f"Retomando com plano existente; {reset} subtarefas reabertas.")
            else:
                plan = await self._plan(wf_id, goal, bundle.text, mcp_status)
                if mode == "plan_only":
                    self.tasks.set_status(wf_id, WF.PLANNED, result_json={"goal": goal, "plan": plan})
                    return WF.PLANNED

            await self._negotiate_assignments(wf_id, goal, plan)
            self.tasks.set_status(wf_id, WF.DELEGATING)
            paused = await self._execute_subtasks(wf_id, goal, mcp_status)

            subs = self.tasks.subtasks(wf_id)
            final_review = None
            if (self.s.final_review and subs and all(s["status"] in ST.DONE for s in subs)
                    and not self.tasks.cancel_requested(wf_id)):
                final_review = await self._final_review(wf_id, goal, subs)
            status = self.tasks.final_status(subs, self.tasks.cancel_requested(wf_id), paused)
            metrics = self._metrics(wf_id)
            self.repo.update_workflow(wf_id, metrics_json=metrics)
            self.tasks.event(wf_id, "orchestrator", "metrics", metrics)
            result = self._result(wf_id, goal, self.repo.get_workflow(wf_id).get("plan") or plan, subs, final_review, metrics)
            self.tasks.set_status(wf_id, status, phase="concluded", result_json=result,
                                  error=None if status == WF.COMPLETED else self._issues_summary(subs, paused))
            return status
        except WorkflowAbort as exc:
            subs = self.tasks.subtasks(wf_id)
            self.tasks.set_status(wf_id, exc.status, error=str(exc), result_json=self._result(wf_id, goal, wf.get("plan"), subs, None, None))
            self.tasks.event(wf_id, "orchestrator", "aborted", str(exc), "error" if exc.status == WF.FAILED else "warning")
            return exc.status

    # ================================================================ 1. planejamento
    async def _plan(self, wf_id: str, goal: str, context_text: str, mcp_status: dict) -> dict:
        self.tasks.set_status(wf_id, WF.PLANNING)
        payload = {"goal": goal, "context_text": context_text, "max_subtasks": self.s.max_subtasks, "mcp_status": mcp_status}
        planner, res = None, None
        for candidate in ("gemini", "deepseek"):
            ok, reason = self.registry.can_accept(candidate)
            if not ok:
                self.tasks.event(wf_id, "orchestrator", "planner_unavailable", f"{candidate}: {reason}", "warning")
                continue
            res = await self._delegate(wf_id, None, candidate, "plan_feature", payload, summary=f"Planejar: {goal[:200]}")
            if res.ok:
                planner = candidate
                break
            if candidate == "gemini" and (res.error_code in UNAVAILABLE_CODES or res.state == "rejected"):
                self.tasks.event(wf_id, "orchestrator", "planner_fallback",
                                 f"Gemini indisponível para planejar ({res.error_code}); DeepSeek assume o planejamento.", "warning")
                continue
            raise WorkflowAbort(f"Planejamento falhou ({res.state}/{res.error_code}): {res.status_message}")
        if planner is None:
            detail = f" ({res.state}/{res.error_code}: {res.status_message})" if res else ""
            raise WorkflowAbort(f"Nenhum agente disponível para planejar{detail}.", WF.BLOCKED)
        try:
            plan_model = Plan.model_validate(res.data or {})
        except ValidationError as exc:
            raise WorkflowAbort(f"Plano inválido: {redact(exc)[:400]}") from exc
        tasks = [t.model_dump() for t in plan_model.tasks[: self.s.max_subtasks]]
        if len(plan_model.tasks) > self.s.max_subtasks:
            self.tasks.event(wf_id, "orchestrator", "plan_truncated", f"Plano truncado para {self.s.max_subtasks} subtarefas.", "warning")
        for i, t in enumerate(tasks, start=1):
            t["id"] = t.get("id") or f"T{i}"
            valid, invalid = [], []
            for f in t.get("files", []):
                try:
                    normalize_file(self.s.project_root, f)
                    valid.append(f)
                except PathOutsideWorkspaceError:
                    invalid.append(f)
            if invalid:
                self.tasks.event(wf_id, "orchestrator", "invalid_paths", f"{t['id']}: caminhos fora do workspace removidos: {invalid}", "warning")
            t["files"] = valid
        fallback = planner != "gemini"
        plan = {**plan_model.model_dump(), "tasks": tasks, "planned_by": planner, "planned_by_fallback": fallback,
                "plan_validated_by": None}
        # Plano de fallback: decisões ficam "proposed" até o Gemini validar o plano na revisão.
        for d in plan_model.decisions:
            self.decisions.add(d.title, d.decision, author=planner, rationale=d.rationale, tags=d.tags, workflow_id=wf_id,
                               status="proposed" if fallback else "accepted")
        self.repo.update_workflow(wf_id, plan_json=plan, planned_by=planner, planned_by_fallback=1 if fallback else 0)
        self.tasks.create_subtasks(wf_id, tasks)
        self.tasks.event(wf_id, planner, "plan", plan, "warning" if fallback else "info")
        if plan_model.decisions:
            await asyncio.to_thread(self.context.refresh_decisions, wf_id)
        return plan

    # ================================================================ 2. atribuição
    async def _negotiate_assignments(self, wf_id: str, goal: str, plan: dict) -> None:
        # Só subtarefas ainda sem decisão e sem implementação (nunca reatribui trabalho já feito).
        subs = [s for s in self.tasks.subtasks(wf_id) if not s.get("assignment") and not s.get("result")]
        if not subs:
            return
        planner = plan.get("planned_by") or "gemini"
        responder = other(planner)
        proposals = [{"task_id": s["plan_id"], "title": s["title"], "category": (s["spec"] or {}).get("category", "other"),
                      "files": s.get("files") or [], "assigned_to": (s["spec"] or {}).get("assigned_to") or s["agent"],
                      "reason": (s["spec"] or {}).get("assignment_reason", "")} for s in subs]
        self.tasks.event(wf_id, planner, "assignment_proposed", proposals)
        responses: dict[str, Any] | None = None
        ok, reason = self.registry.can_accept(responder)
        if not self.s.assignment_negotiation:
            self.tasks.event(wf_id, "orchestrator", "assignment_negotiation_skipped", "ASSIGNMENT_NEGOTIATION=false")
        elif not ok:
            self.tasks.event(wf_id, "orchestrator", "assignment_negotiation_skipped", f"{responder} indisponível: {reason}", "warning")
        else:
            try:
                res = await self._delegate(wf_id, None, responder, "respond_assignment",
                                           {"goal": goal, "planner": planner, "proposals": proposals,
                                            "context_text": self.context.for_workflow(wf_id).text},
                                           summary=f"Aceitar/contestar atribuição de {len(proposals)} subtarefa(s)")
                if res.ok:
                    parsed = AssignmentResponse.model_validate(res.data or {})
                    responses = {r.task_id: r for r in parsed.responses}
                    self.tasks.event(wf_id, responder, "assignment_response", parsed.model_dump())
                else:
                    self.tasks.event(wf_id, responder, "assignment_response_failed", f"{res.state}/{res.error_code}", "warning")
            except (A2ADelegationError, ValidationError) as exc:
                self.tasks.event(wf_id, "orchestrator", "assignment_response_failed", redact(exc), "warning")
        gemini_ok, gemini_reason = self.registry.can_accept("gemini")
        for s in subs:
            spec = dict(s["spec"] or {})
            spec["id"] = s["plan_id"]
            item = responses.get(s["plan_id"]) if responses is not None else None
            decision = resolve_assignment(spec, planner, item, responder if responses is not None else None,
                                          self.rules, None if gemini_ok else gemini_reason)
            self._apply_assignment(wf_id, s["id"], decision)

    def _apply_assignment(self, wf_id: str, sid: str, decision: AssignmentDecision) -> None:
        if decision.reviewer == decision.assigned_to:  # defesa extra; o contrato já impede
            raise ValueError("Autorrevisão bloqueada.")
        reason = decision.proposal_reason if decision.decided_by in {"consensus", "no_negotiation"} and decision.proposal_reason \
            else decision.reason
        self.repo.update_subtask(sid, agent=decision.assigned_to, assigned_to=decision.assigned_to, reviewer=decision.reviewer,
                                 assignment_reason=f"[{decision.decided_by}] {reason}"[:500], assignment_json=decision.model_dump())
        level = "warning" if decision.decided_by == "quota_fallback" else "info"
        self.tasks.event(wf_id, "orchestrator", f"assignment_{decision.decided_by}", decision.model_dump(), level, sid)
        self.repo.add_message(workflow_id=wf_id, subtask_id=sid, direction="decision", sender="orchestrator",
                              recipient=decision.assigned_to, skill="assignment",
                              summary=f"{decision.task_id}: implementa {decision.assigned_to}, revisa {decision.reviewer} "
                                      f"({decision.decided_by}{' ' + decision.rule if decision.rule else ''})",
                              payload=decision.model_dump())

    # ================================================================ 3-6. subtarefas
    async def _execute_subtasks(self, wf_id: str, goal: str, mcp_status: dict) -> bool:
        """Retorna True se o workflow foi pausado pelo limite de subtarefas bloqueadas."""
        guard = 0
        while True:
            guard += 1
            if guard > 200:
                self.tasks.event(wf_id, "orchestrator", "scheduler_guard", "Limite de iterações do agendador atingido.", "error")
                return False
            subs = self.tasks.subtasks(wf_id)
            if self.tasks.cancel_requested(wf_id):
                for s in subs:
                    if s["status"] in (ST.PENDING, ST.INTERRUPTED):
                        self.repo.update_subtask(s["id"], status=ST.CANCELED)
                return False
            blocked = [s for s in subs if s["status"] in ST.BLOCKING]
            if len(blocked) >= self.s.max_blocked_subtasks:
                self.tasks.event(wf_id, "orchestrator", "paused_blocked_limit",
                                 f"{len(blocked)} subtarefa(s) bloqueadas/aguardando revisão (limite {self.s.max_blocked_subtasks}). "
                                 "Workflow pausado; retome quando os agentes estiverem disponíveis.", "warning")
                return True
            by_plan_id = {s["plan_id"]: s for s in subs}
            runnable = []
            for s in subs:
                if s["status"] == ST.AWAITING_REVIEW:
                    reviewer = s.get("reviewer") or other(s.get("assigned_to") or s["agent"])
                    if self.registry.can_accept(reviewer)[0]:
                        runnable.append(s)
                    continue
                if s["status"] not in (ST.PENDING, ST.INTERRUPTED):
                    continue
                deps = [by_plan_id[d] for d in s.get("depends_on") or [] if d in by_plan_id]
                if any(d["status"] in ST.DEP_FAILED for d in deps):
                    self.repo.update_subtask(s["id"], status=ST.BLOCKED, error="Dependência rejeitada/falhou.")
                    self.tasks.event(wf_id, "orchestrator", "dependency_blocked", s["title"], "warning", s["id"])
                    continue
                if all(d["status"] in ST.DONE for d in deps):  # dependência precisa estar APROVADA, não só implementada
                    runnable.append(s)
            if not runnable:
                return False
            batch, used = [], set()
            for s in runnable:
                files = {normalize_file(self.s.project_root, f) for f in s.get("files") or []}
                if len(batch) >= self.s.max_parallel_subtasks:
                    break
                if files & used:
                    continue
                batch.append(s)
                used |= files
            previous = self._previous_results(wf_id)
            await asyncio.gather(*(self._run_subtask(wf_id, goal, s, mcp_status, previous) for s in batch))

    def _previous_results(self, wf_id: str) -> list[str]:
        out = []
        for s in self.tasks.subtasks(wf_id):
            if s["status"] in ST.DONE and isinstance(s.get("result"), dict):
                out.append(f"{s['plan_id']} {s['title']} ({s.get('assigned_to') or s['agent']}): {str(s['result'].get('summary', ''))[:1500]}")
        return out

    async def _run_subtask(self, wf_id: str, goal: str, sub: dict, mcp_status: dict, previous: list[str]) -> None:
        sid = sub["id"]
        review_only = sub["status"] == ST.AWAITING_REVIEW and isinstance(sub.get("result"), dict)
        implementer = sub.get("assigned_to") or sub["agent"]
        if review_only:
            # Quem de fato escreveu o relatório manda; o revisor é sempre o outro.
            implementer = sub["result"].get("implemented_by") or implementer
        if not review_only:
            ok, reason = self.registry.can_accept(implementer)
            if not ok:
                alt = other(implementer)
                alt_ok = self.registry.can_accept(alt)[0]
                if alt_ok and (implementer == "gemini" or self.s.allow_agent_fallback):
                    decided = "quota_fallback" if implementer == "gemini" else "rules"
                    decision = AssignmentDecision(task_id=sub["plan_id"], assigned_to=alt, reviewer=implementer,
                                                  proposed_to=implementer, decided_by=decided,
                                                  reason=f"{implementer} indisponível ao iniciar ({(reason or '')[:200]}); {alt} assume.")
                    self._apply_assignment(wf_id, sid, decision)
                    implementer = alt
                else:
                    self.repo.update_subtask(sid, status=ST.BLOCKED, error=f"{implementer} indisponível: {reason}")
                    self.tasks.event(wf_id, "orchestrator", "agent_unavailable", f"{sub['title']}: {reason}", "warning", sid)
                    return
        reviewer = other(implementer)
        if (sub.get("reviewer") or reviewer) != reviewer or not sub.get("reviewer"):
            self.repo.update_subtask(sid, assigned_to=implementer, reviewer=reviewer, agent=implementer)
        files = list(sub.get("files") or [])
        resources = [self.locks.file_resource(f) for f in files]
        try:
            await self.locks.acquire_wait(resources, implementer, wf_id, sid, self.s.file_lock_wait_seconds)
        except LockConflictError as exc:
            self.repo.update_subtask(sid, status=ST.FAILED, error=str(exc))
            self.tasks.event(wf_id, "orchestrator", "lock_conflict", str(exc), "error", sid)
            return
        reserved = {r.split(":", 1)[1] for r in resources}
        try:
            await self._cycle(wf_id, goal, self.repo.get_subtask(sid), implementer, reviewer, files, reserved, mcp_status,
                              previous, review_only)
        except SubtaskStop:
            pass
        except A2ADelegationError as exc:
            self.repo.update_subtask(sid, status=ST.FAILED, error=f"{exc.code}: {exc}")
            self.tasks.event(wf_id, "orchestrator", "a2a_error", f"{exc.code}: {exc}", "error", sid)
        finally:
            self.locks.release(resources, implementer, sid)

    # ---------------------------------------------------------------- ciclo da subtarefa
    async def _cycle(self, wf_id, goal, sub, implementer, reviewer, files, reserved, mcp_status, previous, review_only) -> None:
        sid, spec = sub["id"], sub["spec"]
        counters = {"review_cycles": int(sub.get("review_cycles") or 0), "check_fix_attempts": int(sub.get("check_fix_attempts") or 0),
                    "impl_attempts": int(sub.get("impl_attempts") or 0)}
        dev_ctx = sub.get("a2a_context_id")
        review_ctx: str | None = None
        refs: list[str] = []
        feedback = self._human_feedback(sub)
        report = ImplementationReport.model_validate(sub["result"]) if review_only else None
        checks = sub.get("checks") or {"status": "not_run"}
        git_changes: list[dict] = []
        last_review: dict | None = sub.get("review")
        last_check_sig: str | None = None
        last_impl_task: str | None = sub.get("last_a2a_task_id")
        ctx = {"wf_id": wf_id, "sid": sid, "implementer": implementer, "reviewer": reviewer, "counters": counters}

        while True:
            if not review_only:
                if self.tasks.cancel_requested(wf_id):
                    self.repo.update_subtask(sid, status=ST.CANCELED)
                    return
                if counters["impl_attempts"] >= self.s.max_implementation_attempts:
                    self._escalate(ctx, "implementation_attempts_exhausted",
                                   f"Teto de {self.s.max_implementation_attempts} tentativas de implementação atingido.",
                                   report, last_review, checks)
                # ---------------- 3. implementação
                counters["impl_attempts"] += 1
                self.repo.update_subtask(sid, status=ST.RUNNING, impl_attempts=counters["impl_attempts"])
                before = await asyncio.to_thread(self.git.snapshot, files)
                res = await self._delegate(
                    wf_id, sid, implementer, "implement_task",
                    {"goal": goal, "task": spec, "context_text": self.context.for_workflow(wf_id).text, "feedback": feedback,
                     "cycle": counters["review_cycles"], "previous_results": previous, "mcp_status": mcp_status, "reviewer": reviewer},
                    summary=(f"Ajuste ({(feedback or {}).get('source', 'review')}): " if feedback else "Implementar: ") + sub["title"],
                    context_id=dev_ctx, reference_task_ids=refs)
                dev_ctx, last_impl_task = res.context_id, res.task_id
                self.repo.update_subtask(sid, a2a_context_id=dev_ctx, last_a2a_task_id=res.task_id)
                if not res.ok:
                    self._subtask_failure(wf_id, sid, implementer, res)
                    return
                report = ImplementationReport.model_validate(res.data or {"structured": False, "status": "partial"})
                changes = await asyncio.to_thread(self.git.changes_since, before, report.files_modified)
                git_changes = self._record_changes(wf_id, sid, implementer, changes, report.files_modified, reserved, before.head)
                for d in report.technical_decisions:
                    self.decisions.add(d[:120], d, author=implementer, status="proposed", workflow_id=wf_id, subtask_id=sid)
                self.repo.update_subtask(sid, result_json={**report.model_dump(), "implemented_by": implementer})
                self.tasks.event(wf_id, implementer, "implementation_report", report.model_dump(), subtask_id=sid)
                self.locks.renew([self.locks.file_resource(f) for f in files], implementer)
                if report.status in {"blocked", "failed"} and not report.issues:
                    self._escalate(ctx, "implementer_blocked", f"{implementer} reportou {report.status} sem detalhes.",
                                   report, last_review, checks)

                # ---------------- 4. checagens automáticas (antes de gastar revisão de LLM)
                self.repo.update_subtask(sid, status="checking")
                targets = sorted(set(files) | set(report.files_modified) | {c["path"] for c in git_changes})
                checks = await asyncio.to_thread(self.checks.run, targets)
                self.repo.update_subtask(sid, checks_json=checks)
                self.tasks.event(wf_id, "orchestrator", f"checks_{checks['status']}",
                                 {"status": checks["status"], "results": checks["results"]},
                                 "warning" if checks["status"] == "failed" else "info", sid)
                if checks["status"] == "failed":
                    if checks["signature"] and checks["signature"] == last_check_sig:
                        self._escalate(ctx, "checks_no_progress", "A mesma falha de checagem se repetiu idêntica (sem progresso).",
                                       report, last_review, checks)
                    last_check_sig = checks["signature"]
                    counters["check_fix_attempts"] += 1
                    self.repo.update_subtask(sid, check_fix_attempts=counters["check_fix_attempts"])
                    if counters["check_fix_attempts"] > self.s.max_check_fix_attempts:
                        self._escalate(ctx, "check_fix_attempts_exhausted",
                                       f"Checagens continuam falhando após {self.s.max_check_fix_attempts} tentativas de correção.",
                                       report, last_review, checks)
                    feedback = {"source": "automated_checks", "attempt": counters["check_fix_attempts"], "failures": checks["failures"]}
                    self.tasks.event(wf_id, "orchestrator", "checks_back_to_implementer",
                                     f"Tentativa de correção {counters['check_fix_attempts']}/{self.s.max_check_fix_attempts}; "
                                     "sem revisão de LLM nesta rodada.", "warning", sid)
                    continue
            review_only = False

            # ---------------- 5. revisão cruzada (somente leitura)
            ok, reason = self.registry.can_accept(reviewer)
            if not ok:
                self.repo.update_subtask(sid, status=ST.AWAITING_REVIEW,
                                         error=f"Aguardando revisão de {reviewer} (indisponível: {reason}). Autorrevisão não é permitida.")
                self.tasks.event(wf_id, "orchestrator", "awaiting_review", f"{sub['title']}: {reviewer} indisponível; {reason}",
                                 "warning", sid)
                return
            review, valid, discarded, rv = await self._review(ctx, goal, spec, report, git_changes, checks, mcp_status,
                                                              files, review_ctx, refs)
            review_ctx = rv.context_id
            last_review = {**review.model_dump(), "findings": [f.model_dump() for f in valid], "discarded_findings": discarded}
            self.repo.update_subtask(sid, review_json=last_review)
            self.tasks.event(wf_id, reviewer, f"review_{review.verdict}", last_review, subtask_id=sid)
            await self._plan_validation(ctx, review, report, last_review, checks)
            for d in review.decisions:
                self.decisions.add(d.title, d.decision, author=reviewer, rationale=d.rationale, tags=d.tags, workflow_id=wf_id,
                                   subtask_id=sid, status="accepted")
            if review.decisions:
                await asyncio.to_thread(self.context.refresh_decisions, wf_id)

            if review.verdict == "approved":
                self.decisions.accept_for_subtask(sid)
                self.repo.update_subtask(sid, status=ST.APPROVED, error=None)
                return
            if review.verdict == "rejected":
                self.decisions.reject_for_subtask(sid)
                self.repo.update_subtask(sid, status=ST.REJECTED, error=review.rejection_reason or review.summary)
                return
            # ---------------- 6. ajuste limitado
            if counters["review_cycles"] >= self.s.max_review_cycles:
                self._escalate(ctx, "review_cycles_exhausted",
                               f"Discordância persiste após {self.s.max_review_cycles} ciclo(s) de ajuste.", report, last_review, checks)
            if self.tasks.cancel_requested(wf_id):
                self.repo.update_subtask(sid, status=ST.CANCELED)
                return
            counters["review_cycles"] += 1
            self.repo.update_subtask(sid, review_cycles=counters["review_cycles"])
            feedback = {"source": "review", "verdict": review.verdict, "findings": [f.model_dump() for f in valid],
                        "requested_changes": review.requested_changes, "questions": review.questions}
            refs = [t for t in (last_impl_task, rv.task_id) if t]
            self.tasks.event(wf_id, reviewer, "correction_requested",
                             {"cycle": counters["review_cycles"], "verdict": review.verdict, "requested_changes": review.requested_changes,
                              "questions": review.questions, "findings": len(valid)}, subtask_id=sid)

    async def _review(self, ctx, goal, spec, report, git_changes, checks, mcp_status, files, review_ctx, refs):
        """Revisão com no máximo UMA reformulação por evidência inválida. Invalida se o revisor alterar arquivos."""
        wf_id, sid, implementer, reviewer = ctx["wf_id"], ctx["sid"], ctx["implementer"], ctx["reviewer"]
        author = (self.repo.get_subtask(sid) or {}).get("result", {}) or {}
        if reviewer == implementer or reviewer == author.get("implemented_by"):
            raise RuntimeError(f"Autorrevisão bloqueada: {reviewer} implementou esta subtarefa.")
        watched = sorted({p for p in (list(files) + list(report.files_modified if report else []))
                          if self._safe_rel(p)})
        validate_plan = self._plan_to_validate(wf_id, reviewer)
        validation_errors, previous_review = None, None
        for attempt in (1, 2):
            hashes_before = self._hashes(watched)
            self.repo.update_subtask(sid, status=ST.REVIEWING)
            self.tasks.set_status(wf_id, WF.REVIEWING)
            rv = await self._delegate(
                wf_id, sid, reviewer, "review_implementation",
                {"goal": goal, "task": spec, "report": report.model_dump() if report else {}, "git_changes": git_changes,
                 "checks": {k: checks.get(k) for k in ("status", "results")} if checks else None,
                 "context_text": self.context.for_workflow(wf_id).text, "cycle": ctx["counters"]["review_cycles"] + 1,
                 "max_cycles": self.s.max_review_cycles, "mcp_status": mcp_status, "implementer": implementer,
                 "validate_plan": validate_plan, "validation_errors": validation_errors, "previous_review": previous_review},
                summary=("Reformular revisão: " if attempt == 2 else "Revisar: ") + spec.get("title", ""),
                context_id=review_ctx, reference_task_ids=refs)
            review_ctx = rv.context_id
            self.tasks.set_status(wf_id, WF.DELEGATING)
            if self._hashes(watched) != hashes_before:
                self.tasks.event(wf_id, "orchestrator", "review_invalidated_files_changed",
                                 f"{reviewer} alterou arquivos durante a revisão somente leitura; veredito descartado.", "error", sid)
                self._escalate(ctx, "reviewer_modified_files", f"{reviewer} alterou arquivos durante a revisão (veredito invalidado).",
                               report, None, checks)
            if not rv.ok:
                code = rv.error_code or rv.state
                if code in UNAVAILABLE_CODES or rv.state == "rejected":
                    self.repo.update_subtask(sid, status=ST.AWAITING_REVIEW,
                                             error=f"Aguardando revisão de {reviewer}: {rv.state}/{code}. Autorrevisão não é permitida.")
                    self.tasks.event(wf_id, "orchestrator", "awaiting_review", f"{reviewer}: {rv.state}/{code}", "warning", sid)
                    raise SubtaskStop()
                if rv.state == "canceled":
                    self.repo.update_subtask(sid, status=ST.CANCELED)
                    raise SubtaskStop()
                self._escalate(ctx, "review_failed", f"Revisão de {reviewer} falhou: {rv.state}/{code}: {rv.status_message[:300]}",
                               report, None, checks)
            review = ReviewResult.model_validate(rv.data or {"verdict": "changes_requested", "structured": False})
            valid, discarded = validate_review(self.s.project_root, review)
            if discarded:
                self.tasks.event(wf_id, "orchestrator", "findings_discarded", discarded, "warning", sid)
            problem = review_problem(review, valid)
            if not problem:
                return review, valid, discarded, rv
            if attempt == 1:
                validation_errors = [problem] + [d["reason"] for d in discarded]
                previous_review = review.model_dump()
                self.tasks.event(wf_id, "orchestrator", "review_reformulation_requested", validation_errors, "warning", sid)
                continue
            self.tasks.event(wf_id, "orchestrator", "review_unverified", problem, "warning", sid)
            self._escalate(ctx, "review_unverified", f"Revisão de {reviewer} sem evidência verificável após reformulação: {problem}",
                           report, {**review.model_dump(), "discarded_findings": discarded}, checks)
        raise RuntimeError("inalcançável")

    async def _plan_validation(self, ctx, review: ReviewResult, report, last_review, checks) -> None:
        wf_id, reviewer = ctx["wf_id"], ctx["reviewer"]
        if not self._plan_to_validate(wf_id, reviewer):
            return
        wf = self.repo.get_workflow(wf_id)
        plan = dict(wf.get("plan") or {})
        if review.plan_valid is False:
            self.tasks.event(wf_id, reviewer, "fallback_plan_rejected", review.plan_concerns, "warning", ctx["sid"])
            self._escalate(ctx, "fallback_plan_rejected",
                           f"{reviewer} não validou o plano feito por {plan.get('planned_by')}: {'; '.join(review.plan_concerns)[:400]}",
                           report, last_review, checks)
        if review.plan_valid is True:
            plan["plan_validated_by"] = reviewer
            self.repo.update_workflow(wf_id, plan_json=plan)
            with self.repo.connect() as con:
                con.execute("UPDATE decisions SET status='accepted' WHERE workflow_id=? AND subtask_id IS NULL AND status='proposed'",
                            (wf_id,))
            self.tasks.event(wf_id, reviewer, "fallback_plan_validated", review.plan_concerns or "sem ressalvas", subtask_id=ctx["sid"])

    def _plan_to_validate(self, wf_id: str, reviewer: str) -> dict | None:
        plan = (self.repo.get_workflow(wf_id) or {}).get("plan") or {}
        if plan.get("planned_by_fallback") and not plan.get("plan_validated_by") and reviewer != plan.get("planned_by"):
            return {"planned_by": plan.get("planned_by"), "plan": {k: plan.get(k) for k in ("summary", "tasks", "risks")}}
        return None

    def _human_feedback(self, sub: dict) -> dict | None:
        hd = sub.get("human_decision")
        if isinstance(hd, dict) and hd.get("action") in {"retry", "reassign"} and not hd.get("consumed"):
            self.repo.update_subtask(sub["id"], human_decision_json={**hd, "consumed": True})
            return {"source": "human", "note": hd.get("note", ""), "last_review": sub.get("review")}
        return None

    def _escalate(self, ctx: dict, code: str, reason: str, report, review: dict | None, checks: dict | None) -> None:
        """Vai para needs_human com o resumo dos dois lados. Sem nova rodada de debate."""
        wf_id, sid = ctx["wf_id"], ctx["sid"]
        rep = report.model_dump() if hasattr(report, "model_dump") else (report or {})
        summary = {
            "code": code,
            "reason": reason,
            "implementer": {"agent": ctx["implementer"], "summary": rep.get("summary"), "files_modified": rep.get("files_modified"),
                            "disputes": rep.get("disputes"), "answers": rep.get("answers"), "issues": rep.get("issues")},
            "reviewer": {"agent": ctx["reviewer"], "verdict": (review or {}).get("verdict"), "summary": (review or {}).get("summary"),
                         "findings": (review or {}).get("findings"), "requested_changes": (review or {}).get("requested_changes"),
                         "questions": (review or {}).get("questions"), "discarded_findings": (review or {}).get("discarded_findings")},
            "checks": {"status": (checks or {}).get("status"), "failures": (checks or {}).get("failures")},
            "counters": dict(ctx["counters"]),
            "options": ["approve", "retry", "reassign", "reject"],
        }
        self.repo.update_subtask(sid, status=ST.NEEDS_HUMAN, escalation_json=summary, error=reason)
        self.tasks.event(wf_id, "orchestrator", "escalated", summary, "warning", sid)
        raise SubtaskStop()

    def _safe_rel(self, path: str) -> str | None:
        try:
            return normalize_file(self.s.project_root, path)
        except PathOutsideWorkspaceError:
            return None

    def _hashes(self, paths: list[str]) -> dict[str, str | None]:
        root = Path(self.s.project_root)
        return {p: _sha(root / self._safe_rel(p)) for p in paths if self._safe_rel(p)}

    def _subtask_failure(self, wf_id: str, sid: str, agent: str, res: DelegationResult) -> None:
        code = res.error_code or res.state
        if res.state == "canceled":
            status = ST.CANCELED
        elif code in UNAVAILABLE_CODES or res.state == "rejected":
            status = ST.BLOCKED
        else:
            status = ST.FAILED
        msg = f"{agent} {res.skill}: {res.state}/{code}: {res.status_message[:800]}"
        self.repo.update_subtask(sid, status=status, error=msg)
        self.tasks.event(wf_id, agent, "subtask_" + status, msg, "error" if status == ST.FAILED else "warning", sid)

    def _record_changes(self, wf_id, sid, agent, git_changes: list[dict], reported: list[str], reserved: set[str],
                        head: str | None = None) -> list[dict]:
        out = []
        seen = set()
        for c in git_changes:
            norm = self._safe_rel(c["path"])
            if not norm:
                continue
            seen.add(norm)
            within = norm in reserved
            self.repo.add_file_change(workflow_id=wf_id, subtask_id=sid, agent=agent, path=c["path"], change_type=c["change_type"],
                                      within_reservation=within, source="git" if self.git.is_repo() else "hash", git_head=head)
            out.append({**c, "within_reservation": within})
            if not within:
                self.tasks.event(wf_id, "orchestrator", "file_conflict",
                                 f"{agent} alterou {c['path']} fora da reserva da subtarefa.", "warning", sid)
        for p in reported:
            norm = self._safe_rel(p)
            if not norm:
                self.tasks.event(wf_id, "orchestrator", "file_conflict", f"{agent} reportou caminho fora do workspace: {p}", "error", sid)
                continue
            if norm not in seen:
                self.repo.add_file_change(workflow_id=wf_id, subtask_id=sid, agent=agent, path=p, change_type="reported_only",
                                          within_reservation=norm in reserved, source="agent_report", git_head=head)
        return out

    # ================================================================ revisão final e métricas
    async def _final_review(self, wf_id: str, goal: str, subs: list[dict]) -> dict | None:
        reviewer = "gemini" if self.registry.can_accept("gemini")[0] else None
        if not reviewer:
            self.tasks.event(wf_id, "orchestrator", "final_review_skipped", "Gemini indisponível.", "warning")
            return None
        summary = [{"title": s["title"], "implemented_by": s.get("assigned_to") or s["agent"], "reviewed_by": s.get("reviewer"),
                    "status": s["status"],
                    "result": (s.get("result") or {}).get("summary") if isinstance(s.get("result"), dict) else None,
                    "review": (s.get("review") or {}).get("verdict") if isinstance(s.get("review"), dict) else None} for s in subs]
        try:
            res = await self._delegate(wf_id, None, reviewer, "final_review",
                                       {"goal": goal, "subtasks": summary, "context_text": self.context.for_workflow(wf_id).text},
                                       summary="Revisão final do workflow")
        except A2ADelegationError as exc:
            self.tasks.event(wf_id, "orchestrator", "final_review_error", str(exc), "error")
            return None
        if res.ok:
            self.tasks.event(wf_id, reviewer, "final_review", res.data)
            return res.data
        self.tasks.event(wf_id, reviewer, "final_review_failed", res.status_message, "warning")
        return None

    def _metrics(self, wf_id: str) -> dict:
        """Métricas para avaliar se a etapa de aceite/contestação muda alguma coisa (item D)."""
        subs = self.tasks.subtasks(wf_id)
        decisions = [s.get("assignment") for s in subs if isinstance(s.get("assignment"), dict)]
        contested = [d for d in decisions if d.get("response") == "counter"]
        rules = [d for d in decisions if d.get("decided_by") == "rules"]
        runs = self.repo.runs_for_workflow(wf_id)
        calls: dict[str, dict[str, int]] = {}
        for r in runs:
            calls.setdefault(r["agent"], {}).setdefault(r["skill"] or "?", 0)
            calls[r["agent"]][r["skill"] or "?"] += 1
        return {
            "subtasks": len(subs),
            "assignment": {
                "negotiated": sum(1 for d in decisions if d.get("response") in {"accept", "counter"}),
                "accepted": sum(1 for d in decisions if d.get("response") == "accept"),
                "contested": len(contested),
                "contested_by": dict(Counter(d.get("responder") for d in contested if d.get("responder"))),
                "tiebreak_winner": dict(Counter(d.get("assigned_to") for d in rules)),
                "tiebreak_rule": dict(Counter(d.get("rule") for d in rules)),
                "outcome_changed_by_negotiation": sum(1 for d in rules if d.get("assigned_to") != d.get("proposed_to")),
                "quota_fallbacks": sum(1 for d in decisions if d.get("decided_by") == "quota_fallback"),
            },
            "per_subtask": [{"id": s["plan_id"], "implementer": s.get("assigned_to"), "reviewer": s.get("reviewer"), "status": s["status"],
                             "review_cycles": s.get("review_cycles"), "check_fix_attempts": s.get("check_fix_attempts"),
                             "impl_attempts": s.get("impl_attempts")} for s in subs],
            "calls_per_agent": calls,
            "calls_total": {a: sum(v.values()) for a, v in calls.items()},
        }

    # ================================================================ utilitários
    async def _delegate(self, wf_id: str, sid: str | None, agent: str, skill: str, payload: dict, *, summary: str,
                        context_id: str | None = None, reference_task_ids: list[str] | None = None,
                        sender: str = "orchestrator") -> DelegationResult:
        async def on_task(task_id: str) -> None:
            if sid and skill == "implement_task":
                self.repo.update_subtask(sid, last_a2a_task_id=task_id)
            self.tasks.event(wf_id, sender, "a2a_sent", f"{skill} -> {agent} (task {task_id})", subtask_id=sid)

        res = await self.a2a.delegate(agent, skill, payload, summary=summary, context_id=context_id,
                                      reference_task_ids=reference_task_ids, workflow_id=wf_id, subtask_id=sid, sender=sender,
                                      should_cancel=self._should_cancel(wf_id), on_task=on_task)
        level = "info" if res.ok else ("warning" if res.state in {"rejected", "canceled"} else "error")
        self.tasks.event(wf_id, agent, "a2a_result", f"{skill}: {res.state}" + (f" ({res.error_code})" if res.error_code else "")
                         + (f" — {res.status_message[:300]}" if res.status_message else ""), level, sid)
        return res

    def _issues_summary(self, subs: list[dict], paused: bool = False) -> str | None:
        issues = [f"{s['title']}: {s['status']} — {s.get('error') or ''}" for s in subs if s["status"] not in ST.DONE]
        if paused:
            issues.insert(0, f"Workflow pausado: limite de {self.s.max_blocked_subtasks} subtarefa(s) bloqueadas atingido.")
        return "\n".join(issues)[:4000] or None

    def _result(self, wf_id: str, goal: str, plan: Any, subs: list[dict], final_review: Any, metrics: dict | None) -> dict:
        return {
            "goal": goal,
            "plan": plan,
            "subtasks": [{k: s.get(k) for k in ("id", "plan_id", "title", "assigned_to", "reviewer", "assignment_reason", "status",
                                                "review_cycles", "check_fix_attempts", "impl_attempts", "result", "review", "checks",
                                                "escalation", "error", "a2a_context_id", "last_a2a_task_id")} for s in subs],
            "final_review": final_review,
            "metrics": metrics,
            "file_changes": self.repo.list_file_changes(wf_id),
        }
