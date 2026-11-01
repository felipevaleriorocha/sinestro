"""Atribuição de subtarefas: proposta (turno 1) -> aceite/contraproposta (turno 2) -> desempate determinístico.

Regra central: quem implementa nunca revisa. `reviewer` é SEMPRE calculado como o outro agente.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.schemas import AssignmentDecision, AssignmentResponseItem

AGENTS = ("gemini", "deepseek")


def other(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError(f"Agente desconhecido: {agent}")
    return "deepseek" if agent == "gemini" else "gemini"


@dataclass
class AssignmentRules:
    default_agent: str = "gemini"
    categories: dict[str, str] = field(default_factory=dict)
    path_rules: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AssignmentRules":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        default = str(data.get("default_agent", "gemini")).lower()
        cats = {str(k).lower(): str(v).lower() for k, v in (data.get("categories") or {}).items() if str(v).lower() in AGENTS}
        rules = [r for r in (data.get("path_rules") or []) if isinstance(r, dict) and str(r.get("agent", "")).lower() in AGENTS
                 and r.get("pattern")]
        return cls(default if default in AGENTS else "gemini", cats, rules)

    @staticmethod
    def _match(path: str, pattern: str) -> bool:
        path = path.replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/"):
            tail = pattern[3:]
            return fnmatch.fnmatch(path, tail) or any(fnmatch.fnmatch("/".join(path.split("/")[i:]), tail)
                                                       for i in range(1, path.count("/") + 1))
        return False

    def decide(self, category: str, files: list[str]) -> tuple[str, str]:
        """Retorna (agente, regra aplicada)."""
        cat = (category or "").lower()
        if cat in self.categories:
            return self.categories[cat], f"category:{cat}"
        for rule in self.path_rules:
            for f in files or []:
                if self._match(f, rule["pattern"]):
                    return str(rule["agent"]).lower(), f"path:{rule['pattern']}"
        return self.default_agent, "default_agent"


def resolve_assignment(task: dict, proposed_by: str, response: AssignmentResponseItem | None, responder: str | None,
                       rules: AssignmentRules, gemini_unavailable_reason: str | None) -> AssignmentDecision:
    """Decisão final de uma subtarefa. No máximo 2 turnos (proposta + resposta); o desempate é por regras, sem 3ª rodada."""
    proposed = task.get("assigned_to") or task.get("agent") or "deepseek"
    base = {"task_id": task.get("id", ""), "proposed_by": proposed_by, "proposed_to": proposed,
            "proposal_reason": task.get("assignment_reason", ""), "responder": responder}
    if response is None:
        assigned, decided_by, rule, reason = proposed, "no_negotiation", "", "Sem resposta do outro agente; mantida a proposta."
        base["response"] = "none"
    elif response.decision == "accept" or response.proposed_agent == proposed:
        # Contraproposta que pede o mesmo agente proposto equivale a aceite.
        assigned, decided_by, rule, reason = proposed, "consensus", "", "Proposta aceita."
        base["response"] = "accept"
    else:
        assigned, rule = rules.decide(task.get("category", "other"), task.get("files", []))
        decided_by = "rules"
        reason = f"Contestação de {responder}; desempate por {rule} -> {assigned}."
        base.update(response="counter", counter_reason=response.reason)
    if assigned == "gemini" and gemini_unavailable_reason:
        reason = f"Gemini sem cota/indisponível ({gemini_unavailable_reason[:200]}); DeepSeek assume. Decisão anterior: {decided_by}."
        assigned, decided_by = "deepseek", "quota_fallback"
    return AssignmentDecision(**base, assigned_to=assigned, reviewer=other(assigned), decided_by=decided_by, rule=rule, reason=reason)
