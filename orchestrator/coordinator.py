"""Coordenador do fluxo multiagente (todas as interações entre agentes passam por A2A).

Fases: 1 Planejamento (Gemini) -> 2 Delegação -> 3 Execução (DeepSeek) -> 4 Revisão (Gemini)
       -> 5 Correção (loop limitado por MAX_REVIEW_CYCLES) -> 6 Conclusão.
"""
from __future__ import annotations

import asyncio
import logging
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
from orchestrator.file_locks import FileLockManager, LockConflictError, PathOutsideWorkspaceError, normalize_file
from orchestrator.git_integration import GitInspector
from orchestrator.schemas import ImplementationReport, Plan, ReviewResult
from orchestrator.task_manager import ST, WF, TaskManager

log = logging.getLogger("orchestrator.coordinator")


class WorkflowAbort(RuntimeError):
    def __init__(self, message: str, status: str = WF.FAILED):
        super().__init__(message)
        self.status = status


class Coordinator:
    def __init__(self, settings: Settings, repo: Repository, tasks: TaskManager, registry: AgentRegistry,
                 a2a: A2AAgentClient, context: ContextManager, decisions: DecisionStore, locks: FileLockManager,
                 git: GitInspector, mcp: MCPServerRegistry, checks=None, rules=None):
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
        self.checks = checks
        self.rules = rules
        self._running: dict[str, asyncio.Task] = {}
        self._workflow_gate = asyncio.Semaphore(1)  # workflows serializados (atribuição de mudanças via Git fica confiável)

    # ---------------------------------------------------------------- API pública
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

    # ---------------------------------------------------------------- execução
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
                self.tasks.event(wf_id, "orchestrator", "resume", f"Retomando com plano existente; {reset} subtarefas pendentes.")
            else:
                plan = await self._plan(wf_id, goal, bundle.text, mcp_status)
                if mode == "plan_only":
                    self.tasks.set_status(wf_id, WF.PLANNED, result_json={"goal": goal, "plan": plan})
                    return WF.PLANNED

            self.tasks.set_status(wf_id, WF.DELEGATING)
            await self._execute_subtasks(wf_id, goal, mcp_status)

            subs = self.tasks.subtasks(wf_id)
            final_review = None
            if self.s.final_review and any(s["status"] in ST.DONE for s in subs) and not self.tasks.cancel_requested(wf_id):
                final_review = await self._final_review(wf_id, goal, subs)
            status = self.tasks.final_status(subs, self.tasks.cancel_requested(wf_id))
            result = self._result(wf_id, goal, plan, subs, final_review)
            self.tasks.set_status(wf_id, status, phase="concluded", result_json=result,
                                  error=None if status == WF.COMPLETED else self._issues_summary(subs))
            return status
        except WorkflowAbort as exc:
            self.tasks.set_status(wf_id, exc.status, error=str(exc), result_json=self._result(wf_id, goal, wf.get("plan"), self.tasks.subtasks(wf_id), None))
            self.tasks.event(wf_id, "orchestrator", "aborted", str(exc), "error" if exc.status == WF.FAILED else "warning")
            return exc.status

    # ---------------------------------------------------------------- fase 1
    async def _plan(self, wf_id: str, goal: str, context_text: str, mcp_status: dict) -> dict:
        self.tasks.set_status(wf_id, WF.PLANNING)
        agent, reason = self.registry.select("gemini", "plan_feature")
        if not agent:
            raise WorkflowAbort(f"Tech Lead indisponível para planejar: {reason}", WF.BLOCKED)
        res = await self._delegate(wf_id, None, agent, "plan_feature",
                                   {"goal": goal, "context_text": context_text, "max_subtasks": self.s.max_subtasks, "mcp_status": mcp_status},
                                   summary=f"Planejar: {goal[:200]}", sender="orchestrator")
        if not res.ok:
            raise WorkflowAbort(f"Planejamento falhou ({res.state}/{res.error_code}): {res.status_message}",
                                WF.BLOCKED if res.error_code in {"quota_exceeded", "agent_unavailable", "connection_failed", "model_unavailable"} else WF.FAILED)
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
        plan = {**plan_model.model_dump(), "tasks": tasks}
        for d in plan_model.decisions:
            self.decisions.add(d.title, d.decision, author="gemini", rationale=d.rationale, tags=d.tags, workflow_id=wf_id, status="accepted")
        self.repo.update_workflow(wf_id, plan_json=plan)
        self.tasks.create_subtasks(wf_id, tasks)
        self.tasks.event(wf_id, "gemini", "plan", plan)
        if plan_model.decisions:
            await asyncio.to_thread(self.context.refresh_decisions, wf_id)
        return plan

    # ---------------------------------------------------------------- fases 2-5
    async def _execute_subtasks(self, wf_id: str, goal: str, mcp_status: dict) -> None:
        while True:
            if self.tasks.cancel_requested(wf_id):
                for s in self.tasks.subtasks(wf_id):
                    if s["status"] in (ST.PENDING, ST.INTERRUPTED):
                        self.repo.update_subtask(s["id"], status=ST.CANCELED)
                return
            subs = self.tasks.subtasks(wf_id)
            pending = [s for s in subs if s["status"] in (ST.PENDING, ST.INTERRUPTED)]
            if not pending:
                return
            by_plan_id = {s["plan_id"]: s for s in subs}
            runnable, waiting_failed = [], []
            for s in pending:
                deps = [by_plan_id.get(d) for d in s.get("depends_on") or [] if d in by_plan_id]
                if any(d["status"] in ST.FINAL and d["status"] not in ST.DONE for d in deps):
                    waiting_failed.append(s)
                elif all(d["status"] in ST.DONE for d in deps):
                    runnable.append(s)
            for s in waiting_failed:
                self.repo.update_subtask(s["id"], status=ST.BLOCKED, error="Dependência não concluída com sucesso.")
                self.tasks.event(wf_id, "orchestrator", "dependency_blocked", s["title"], "warning", s["id"])
            if not runnable:
                if waiting_failed:
                    continue
                for s in pending:
                    self.repo.update_subtask(s["id"], status=ST.BLOCKED, error="Dependências circulares ou inexistentes.")
                return
            # Paralelismo apenas para subtarefas independentes (arquivos disjuntos, sem dependência entre si).
            batch, used_files = [], set()
            for s in runnable:
                files = {normalize_file(self.s.project_root, f) for f in s.get("files") or []}
                if len(batch) >= self.s.max_parallel_subtasks:
                    break
                if files & used_files:
                    continue
                batch.append(s)
                used_files |= files
            previous = self._previous_results(wf_id)
            await asyncio.gather(*(self._run_subtask(wf_id, goal, s, mcp_status, previous) for s in batch))

    def _previous_results(self, wf_id: str) -> list[str]:
        out = []
        for s in self.tasks.subtasks(wf_id):
            if s["status"] in ST.DONE and isinstance(s.get("result"), dict):
                out.append(f"{s['plan_id']} {s['title']} ({s['agent']}): {str(s['result'].get('summary', ''))[:1500]}")
        return out

    async def _run_subtask(self, wf_id: str, goal: str, sub: dict, mcp_status: dict, previous: list[str]) -> None:
        sid = sub["id"]
        agent, reason = self.registry.select(sub["agent"], "implement_task")
        if not agent:
            self.repo.update_subtask(sid, status=ST.BLOCKED, error=reason)
            self.tasks.event(wf_id, "orchestrator", "agent_unavailable", f"{sub['title']}: {reason}", "warning", sid)
            return
        if agent != sub["agent"]:
            self.tasks.event(wf_id, "orchestrator", "fallback", f"{sub['title']}: {sub['agent']} indisponível, usando {agent}.", "warning", sid)
            self.repo.update_subtask(sid, agent=agent)
        files = list(sub.get("files") or [])
        resources = [self.locks.file_resource(f) for f in files]
        try:
            await self.locks.acquire_wait(resources, agent, wf_id, sid, self.s.file_lock_wait_seconds)
        except LockConflictError as exc:
            self.repo.update_subtask(sid, status=ST.FAILED, error=str(exc))
            self.tasks.event(wf_id, "orchestrator", "lock_conflict", str(exc), "error", sid)
            return
        reserved = {r.split(":", 1)[1] for r in resources}
        try:
            await self._implement_review_loop(wf_id, goal, sub, agent, files, reserved, mcp_status, previous)
        except A2ADelegationError as exc:
            self.repo.update_subtask(sid, status=ST.FAILED, error=f"{exc.code}: {exc}")
            self.tasks.event(wf_id, "orchestrator", "a2a_error", f"{exc.code}: {exc}", "error", sid)
        finally:
            self.locks.release(resources, agent, sid)

    async def _implement_review_loop(self, wf_id, goal, sub, agent, files, reserved, mcp_status, previous) -> None:
        sid = sub["id"]
        spec = sub["spec"]
        dev_ctx = sub.get("a2a_context_id")
        review_ctx: str | None = None
        feedback: dict | None = None
        refs: list[str] = []
        cycle = int(sub.get("review_cycles") or 0)
        while True:
            bundle = self.context.for_workflow(wf_id)
            before = await asyncio.to_thread(self.git.snapshot, files)
            self.repo.update_subtask(sid, status=ST.RUNNING)
            delegated_by = "gemini" if agent == "deepseek" else "orchestrator"
            res = await self._delegate(
                wf_id, sid, agent, "implement_task",
                {"goal": goal, "task": spec, "context_text": bundle.text, "feedback": feedback, "cycle": cycle,
                 "previous_results": previous, "mcp_status": mcp_status},
                summary=(f"Correção ciclo {cycle}: " if feedback else "Implementar: ") + sub["title"],
                context_id=dev_ctx, reference_task_ids=refs, sender=delegated_by,
            )
            dev_ctx = res.context_id
            self.repo.update_subtask(sid, a2a_context_id=dev_ctx, last_a2a_task_id=res.task_id)
            if not res.ok:
                self._subtask_failure(wf_id, sid, agent, res)
                return
            report = ImplementationReport.model_validate(res.data or {"structured": False, "status": "partial"})
            changes = await asyncio.to_thread(self.git.changes_since, before, report.files_modified)
            git_changes = self._record_changes(wf_id, sid, agent, changes, report.files_modified, reserved, before.head)
            for d in report.technical_decisions:
                self.decisions.add(d[:120], d, author=agent, status="proposed", workflow_id=wf_id, subtask_id=sid)
            self.repo.update_subtask(sid, result_json=report.model_dump())
            self.tasks.event(wf_id, agent, "implementation_report", report.model_dump(), subtask_id=sid)
            self.locks.renew([self.locks.file_resource(f) for f in files], agent)

            if agent == "gemini":
                self.repo.update_subtask(sid, status=ST.COMPLETED)
                return
            if report.status in {"blocked", "failed"} and not report.issues:
                self.repo.update_subtask(sid, status=ST.NEEDS_ATTENTION, error=f"Desenvolvedor reportou {report.status} sem detalhes.")
                return

            reviewer, reason = self.registry.select("gemini", "review_implementation")
            if not reviewer:
                self.repo.update_subtask(sid, status=ST.NEEDS_ATTENTION, error=f"Revisor indisponível: {reason}")
                return
            cycle += 1
            self.repo.update_subtask(sid, status=ST.REVIEWING, review_cycles=cycle)
            self.tasks.set_status(wf_id, WF.REVIEWING)
            rv = await self._delegate(
                wf_id, sid, reviewer, "review_implementation",
                {"goal": goal, "task": spec, "report": report.model_dump(), "git_changes": git_changes,
                 "context_text": self.context.for_workflow(wf_id).text, "cycle": cycle,
                 "max_cycles": self.s.max_review_cycles + 1, "mcp_status": mcp_status},
                summary=f"Revisar ciclo {cycle}: {sub['title']}", context_id=review_ctx, reference_task_ids=[res.task_id],
                sender="orchestrator",
            )
            review_ctx = rv.context_id
            self.tasks.set_status(wf_id, WF.DELEGATING)
            if not rv.ok:
                self._subtask_failure(wf_id, sid, reviewer, rv, review=True)
                return
            review = ReviewResult.model_validate(rv.data or {"verdict": "changes_requested", "structured": False})
            self.repo.update_subtask(sid, review_json=review.model_dump())
            self.tasks.event(wf_id, "gemini", f"review_{review.verdict}", review.model_dump(), subtask_id=sid)
            for d in review.decisions:
                self.decisions.add(d.title, d.decision, author="gemini", rationale=d.rationale, tags=d.tags, workflow_id=wf_id,
                                   subtask_id=sid, status="accepted")
            if review.decisions:
                await asyncio.to_thread(self.context.refresh_decisions, wf_id)

            if review.verdict == "approved":
                self.decisions.accept_for_subtask(sid)
                self.repo.update_subtask(sid, status=ST.APPROVED)
                return
            if review.verdict == "rejected":
                self.decisions.reject_for_subtask(sid)
                self.repo.update_subtask(sid, status=ST.REJECTED, error=review.rejection_reason or review.summary)
                return
            if not review.structured:
                self.repo.update_subtask(sid, status=ST.NEEDS_ATTENTION, error="Revisão sem JSON estruturado; intervenção humana necessária.")
                return
            if cycle > self.s.max_review_cycles:
                self.repo.update_subtask(sid, status=ST.NEEDS_ATTENTION,
                                         error=f"Limite de {self.s.max_review_cycles} ciclos de correção atingido; ajustes pendentes.")
                self.tasks.event(wf_id, "orchestrator", "review_limit", f"{sub['title']}: limite de ciclos atingido.", "warning", sid)
                return
            if self.tasks.cancel_requested(wf_id):
                self.repo.update_subtask(sid, status=ST.CANCELED)
                return
            feedback = review.model_dump()
            refs = [res.task_id, rv.task_id]
            self.tasks.event(wf_id, "gemini", "correction_requested", {"cycle": cycle, "verdict": review.verdict,
                                                                       "requested_changes": review.requested_changes,
                                                                       "questions": review.questions}, subtask_id=sid)

    def _subtask_failure(self, wf_id: str, sid: str, agent: str, res: DelegationResult, review: bool = False) -> None:
        code = res.error_code or res.state
        if res.state == "canceled":
            status = ST.CANCELED
        elif code in {"quota_exceeded", "agent_unavailable", "connection_failed", "model_unavailable", "contract_not_verified"} or res.state == "rejected":
            status = ST.BLOCKED
        else:
            status = ST.FAILED if not review else ST.NEEDS_ATTENTION
        msg = f"{agent} {res.skill}: {res.state}/{code}: {res.status_message[:800]}"
        self.repo.update_subtask(sid, status=status, error=msg)
        self.tasks.event(wf_id, agent, "subtask_" + status, msg, "error" if status == ST.FAILED else "warning", sid)

    def _record_changes(self, wf_id, sid, agent, git_changes: list[dict], reported: list[str], reserved: set[str],
                        head: str | None = None) -> list[dict]:
        out = []
        seen = set()
        for c in git_changes:
            try:
                norm = normalize_file(self.s.project_root, c["path"])
            except PathOutsideWorkspaceError:
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
            try:
                norm = normalize_file(self.s.project_root, p)
            except PathOutsideWorkspaceError:
                self.tasks.event(wf_id, "orchestrator", "file_conflict", f"{agent} reportou caminho fora do workspace: {p}", "error", sid)
                continue
            if norm not in seen:
                self.repo.add_file_change(workflow_id=wf_id, subtask_id=sid, agent=agent, path=p, change_type="reported_only",
                                          within_reservation=norm in reserved, source="agent_report", git_head=head)
        return out

    # ---------------------------------------------------------------- fase 6
    async def _final_review(self, wf_id: str, goal: str, subs: list[dict]) -> dict | None:
        reviewer, reason = self.registry.select("gemini", "final_review")
        if not reviewer:
            self.tasks.event(wf_id, "orchestrator", "final_review_skipped", reason or "", "warning")
            return None
        summary = [{"title": s["title"], "agent": s["agent"], "status": s["status"],
                    "result": (s.get("result") or {}).get("summary") if isinstance(s.get("result"), dict) else None,
                    "review": (s.get("review") or {}).get("verdict") if isinstance(s.get("review"), dict) else None} for s in subs]
        try:
            res = await self._delegate(wf_id, None, reviewer, "final_review",
                                       {"goal": goal, "subtasks": summary, "context_text": self.context.for_workflow(wf_id).text},
                                       summary="Revisão final do workflow", sender="orchestrator")
        except A2ADelegationError as exc:
            self.tasks.event(wf_id, "orchestrator", "final_review_error", str(exc), "error")
            return None
        if res.ok:
            self.tasks.event(wf_id, "gemini", "final_review", res.data)
            return res.data
        self.tasks.event(wf_id, "gemini", "final_review_failed", res.status_message, "warning")
        return None

    # ---------------------------------------------------------------- utilitários
    async def _delegate(self, wf_id: str, sid: str | None, agent: str, skill: str, payload: dict, *, summary: str,
                        context_id: str | None = None, reference_task_ids: list[str] | None = None,
                        sender: str = "orchestrator") -> DelegationResult:
        async def on_task(task_id: str) -> None:
            if sid:
                self.repo.update_subtask(sid, last_a2a_task_id=task_id)
            self.tasks.event(wf_id, sender, "a2a_sent", f"{skill} -> {agent} (task {task_id})", subtask_id=sid)

        res = await self.a2a.delegate(agent, skill, payload, summary=summary, context_id=context_id,
                                      reference_task_ids=reference_task_ids, workflow_id=wf_id, subtask_id=sid, sender=sender,
                                      should_cancel=self._should_cancel(wf_id), on_task=on_task)
        level = "info" if res.ok else ("warning" if res.state in {"rejected", "canceled"} else "error")
        self.tasks.event(wf_id, agent, "a2a_result", f"{skill}: {res.state}" + (f" ({res.error_code})" if res.error_code else "")
                         + (f" — {res.status_message[:300]}" if res.status_message else ""), level, sid)
        return res

    def _issues_summary(self, subs: list[dict]) -> str | None:
        issues = [f"{s['title']}: {s['status']} — {s.get('error') or ''}" for s in subs if s["status"] not in ST.DONE]
        return "\n".join(issues)[:4000] or None

    def _result(self, wf_id: str, goal: str, plan: Any, subs: list[dict], final_review: Any) -> dict:
        return {
            "goal": goal,
            "plan": plan,
            "subtasks": [{k: s.get(k) for k in ("id", "plan_id", "title", "agent", "status", "review_cycles", "result", "review", "error",
                                                "a2a_context_id", "last_a2a_task_id")} for s in subs],
            "final_review": final_review,
            "file_changes": self.repo.list_file_changes(wf_id),
        }
