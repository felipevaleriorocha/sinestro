"""Estados de workflows/subtarefas e transições persistidas."""
from __future__ import annotations

import uuid
from typing import Any

from core.version import __version__
from database.repository import Repository

VERSION = __version__


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
    NEEDS_HUMAN = "needs_human"    # discordância após os ciclos / evidência não verificável; aguarda decisão humana
    PAUSED = "paused"              # limite de subtarefas bloqueadas atingido
    COMPLETED_UNREVIEWED = "completed_unreviewed"  # subtarefas aprovadas, mas a revisão final não aconteceu
    RESUMABLE = {BLOCKED, FAILED, INTERRUPTED, COMPLETED_WITH_ISSUES, PLANNED, CANCELED, NEEDS_HUMAN, PAUSED,
                 COMPLETED_UNREVIEWED}
    ACTIVE = {QUEUED, PLANNING, DELEGATING, REVIEWING}


class ST:
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    COMPLETED = "completed"        # legado v0.3 (Gemini implementava sem revisão cruzada)
    REJECTED = "rejected"
    NEEDS_ATTENTION = "needs_attention"  # legado v0.3
    NEEDS_HUMAN = "needs_human"
    AWAITING_REVIEW = "awaiting_review"  # implementada; revisor (o outro agente) indisponível — nunca autorrevisão
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"
    DONE = {APPROVED, COMPLETED}
    FINAL = {APPROVED, COMPLETED, REJECTED, NEEDS_ATTENTION, BLOCKED, FAILED, CANCELED, NEEDS_HUMAN}
    DEP_FAILED = {REJECTED, FAILED, CANCELED}
    BLOCKING = {BLOCKED, AWAITING_REVIEW}


class FR:
    """Estado da revisão final (`final_review_state`), sempre gravado — nenhum estado final silencioso."""
    PASSED = "passed"
    DISABLED = "disabled"                    # FINAL_REVIEW=false (escolha do usuário; selo no painel)
    SKIPPED = "skipped"                      # revisor sem cota/indisponível
    SKIPPED_BY_POLICY = "skipped_by_policy"  # modo simples com 1 subtarefa aprovada
    FAILED = "failed"                        # erro técnico na chamada/resposta
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"                # achados sem evidência mesmo após a reformulação
    INVALIDATED = "invalidated"              # revisor alterou arquivos durante a revisão somente leitura
    NOT_REACHED = "not_reached"              # nem todas as subtarefas foram aprovadas; revisão final não se aplica
    ACCEPTED_BY_HUMAN = "accepted_by_human"  # usuário aceitou apesar do parecer
    REJECTED_BY_HUMAN = "rejected_by_human"  # usuário confirmou a rejeição
    CLEAN = {PASSED, DISABLED, SKIPPED_BY_POLICY, ACCEPTED_BY_HUMAN}
    UNREVIEWED = {SKIPPED, FAILED}
    NEEDS_HUMAN = {CHANGES_REQUESTED, REJECTED, UNVERIFIED, INVALIDATED}


def final_status(subtasks: list[dict], cancelled: bool, paused: bool, final_review_state: str | None) -> str:
    """Decisão ÚNICA do status final do workflow (função pura, sem I/O).

    `completed` só é possível com todas as subtarefas concluídas E revisão final passed/disabled/accepted_by_human.
    """
    statuses = [s["status"] for s in subtasks]
    if cancelled or ST.CANCELED in statuses:
        return WF.CANCELED
    if statuses and all(s in ST.DONE for s in statuses):
        if final_review_state in FR.CLEAN:
            return WF.COMPLETED
        if final_review_state in FR.NEEDS_HUMAN:
            return WF.NEEDS_HUMAN
        if final_review_state == FR.REJECTED_BY_HUMAN:
            return WF.FAILED
        return WF.COMPLETED_UNREVIEWED  # skipped, failed, None ou qualquer valor desconhecido
    if ST.NEEDS_HUMAN in statuses:
        return WF.NEEDS_HUMAN
    if paused:
        return WF.PAUSED
    if any(s in ST.BLOCKING or s == ST.PENDING for s in statuses):
        return WF.BLOCKED
    if any(s in ST.DONE for s in statuses):
        return WF.COMPLETED_WITH_ISSUES
    return WF.FAILED


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
                "agent": t.get("assigned_to") or t.get("agent", "deepseek"),
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
        """Volta para `pending` o que pode ser refeito. Mantém: aprovadas/rejeitadas, `awaiting_review`
        (retoma direto na revisão) e `needs_human` sem decisão (aguardam POST .../decision)."""
        n = 0
        self.repo.update_workflow(wf_id, cancel_requested=0, error=None)
        keep = ST.DONE | {ST.REJECTED, ST.AWAITING_REVIEW, ST.NEEDS_HUMAN, ST.PENDING}
        for s in self.subtasks(wf_id):
            if s["status"] not in keep:
                self.repo.update_subtask(s["id"], status=ST.PENDING, error=None)
                n += 1
        return n

