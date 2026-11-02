"""Métricas e resultado final do workflow (mixin do Coordinator).

Funções de leitura/agregação sobre `tasks` e `repo`, sem efeitos colaterais no fluxo de execução.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from orchestrator.task_manager import ST


class ReportingMixin:
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
        cost_summary = self.repo.get_cost_summary(wf_id)
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
            "final_review": {k: ((self.repo.get_workflow(wf_id) or {}).get("final_review") or {}).get(k)
                             for k in ("state", "attempt", "reviewer", "self_authored_subtasks")},
            "calls_per_agent": calls,
            "calls_total": {a: sum(v.values()) for a, v in calls.items()},
            "cost_summary": cost_summary,
        }

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
