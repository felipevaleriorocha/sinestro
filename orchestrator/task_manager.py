"""Estados de workflows/subtarefas e transições persistidas."""
from __future__ import annotations

import uuid
from typing import Any

from database.repository import Repository

VERSION = "0.3.0"


class WF:
    QUEUED = "queued"
    PLANNING = "planning"
    PLANNED = "planned"            # modo plan_only
    DELEGATING = "delegating"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    COMPLETED_WITH_ISSUES = "completed_with_issues"
    BLOCKED = "blocked"            # agente indisponível/quota; pode ser retomado
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"    # serviço reiniciado no meio
    RESUMABLE = {BLOCKED, FAILED, INTERRUPTED, COMPLETED_WITH_ISSUES, PLANNED, CANCELED}
    ACTIVE = {QUEUED, PLANNING, DELEGATING, REVIEWING}


class ST:
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    COMPLETED = "completed"        # executada pelo Gemini (sem revisão cruzada)
    REJECTED = "rejected"
    NEEDS_ATTENTION = "needs_attention"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"
    DONE = {APPROVED, COMPLETED}
    FINAL = {APPROVED, COMPLETED, REJECTED, NEEDS_ATTENTION, BLOCKED, FAILED, CANCELED}


class TaskManager:
    def __init__(self, repo: Repository):
        self.repo = repo

    def create_workflow(self, goal: str, mode: str = "full") -> str:
        wf_id = str(uuid.uuid4())
        self.repo.create_workflow(wf_id, goal, mode, VERSION)
        self.repo.add_event(wf_id, "orchestrator", "created", f"Workflow criado (modo {mode}).")
        return wf_id

    def set_status(self, wf_id: str, status: str, phase: str | None = None, **fields: Any) -> None:
        self.repo.update_workflow(wf_id, status=status, phase=phase or status, **fields)
        self.repo.add_event(wf_id, "orchestrator", "status", f"{status}" + (f" ({phase})" if phase and phase != status else ""))

    def event(self, wf_id: str, agent: str, event: str, content: Any, level: str = "info", subtask_id: str | None = None) -> None:
        self.repo.add_event(wf_id, agent, event, content, level, subtask_id)

    def create_subtasks(self, wf_id: str, tasks: list[dict]) -> list[dict]:
        created = []
        for seq, t in enumerate(tasks, start=1):
            sub = {
                "id": f"{wf_id[:8]}-{seq:02d}-{uuid.uuid4().hex[:6]}",
                "workflow_id": wf_id,
                "seq": seq,
                "plan_id": t.get("id") or f"T{seq}",
                "title": t.get("title") or f"Subtarefa {seq}",
                "agent": t.get("agent", "deepseek"),
                "spec": t,
                "files": t.get("files", []),
                "depends_on": t.get("depends_on", []),
            }
            self.repo.create_subtask(sub)
            created.append(sub)
        return created

    def subtasks(self, wf_id: str) -> list[dict]:
        return self.repo.list_subtasks(wf_id)

    def cancel_requested(self, wf_id: str) -> bool:
        wf = self.repo.get_workflow(wf_id)
        return bool(wf and wf.get("cancel_requested"))

    def request_cancel(self, wf_id: str) -> None:
        self.repo.update_workflow(wf_id, cancel_requested=1)
        self.repo.add_event(wf_id, "orchestrator", "cancel_requested", "Cancelamento solicitado.", "warning")

    def reset_for_resume(self, wf_id: str) -> int:
        n = 0
        self.repo.update_workflow(wf_id, cancel_requested=0, error=None)
        for s in self.subtasks(wf_id):
            if s["status"] not in ST.DONE and s["status"] != ST.REJECTED:
                self.repo.update_subtask(s["id"], status=ST.PENDING, error=None)
                n += 1
        return n

    @staticmethod
    def final_status(subtasks: list[dict], cancel_requested: bool) -> str:
        statuses = [s["status"] for s in subtasks]
        if cancel_requested or ST.CANCELED in statuses:
            return WF.CANCELED
        if statuses and all(s in ST.DONE for s in statuses):
            return WF.COMPLETED
        if ST.BLOCKED in statuses:
            return WF.BLOCKED
        if any(s in ST.DONE for s in statuses):
            return WF.COMPLETED_WITH_ISSUES
        return WF.FAILED
