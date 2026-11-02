"""Atribuição de subtarefas: proposta (turno 1) -> aceite/contraproposta (turno 2) -> desempate determinístico.

Regra central: quem implementa nunca revisa. `reviewer` é SEMPRE calculado como o outro agente.
"""
from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.capabilities import CapabilityRegistry, ModelCapability
from orchestrator.schemas import AssignmentDecision, AssignmentResponseItem

log = logging.getLogger("orchestrator.assignment")

AGENTS = ("gemini", "deepseek")


def other(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError(f"Agente desconhecido: {agent}")
    return "deepseek" if agent == "gemini" else "gemini"


MIN_DATA_DRIVEN_SAMPLES = 5


class IntelligentRouter:
    """Roteador inteligente ciente de dados reais de execução, capacidades de modelos e otimização de custo."""

    HIGH_REASONING_CATEGORIES = {"architecture", "security", "core", "complex_logic", "planning"}
    COST_EFFICIENT_CATEGORIES = {"asset", "assets", "boilerplate", "docs", "ui", "refactor", "tests"}

    @classmethod
    def route(
        cls,
        category: str,
        files: list[str] | None = None,
        registry: Any | None = None,
        default_agent: str = "gemini",
        repo: Any | None = None,
    ) -> tuple[str, str]:
        """Seleciona o melhor agente com base em dados empíricos de execução, capacidades e custo."""
        cat = (category or "").lower()

        # 1. Roteamento Orientado a Dados (Fase 4): decide por desempenho real se houver amostras
        if repo and hasattr(repo, "get_routing_stats"):
            try:
                stats = repo.get_routing_stats(category=cat)
                candidates_scores = []
                for s in stats:
                    name = s["agent"]
                    if registry:
                        ok, _ = registry.can_accept(name)
                        if not ok:
                            continue
                    if s.get("samples_count", 0) >= MIN_DATA_DRIVEN_SAMPLES:
                        fp_rate = s.get("first_pass_rate", 0.0)
                        cycle_score = 1.0 / (1.0 + s.get("avg_review_cycles", 0.0))
                        speed_score = 1.0 / (1.0 + (s.get("avg_duration_seconds", 0.0) / 60.0))
                        total_score = 0.55 * fp_rate + 0.25 * cycle_score + 0.20 * speed_score
                        candidates_scores.append((name, total_score, fp_rate, s.get("avg_review_cycles", 0.0), s["samples_count"]))

                if candidates_scores:
                    candidates_scores.sort(key=lambda x: x[1], reverse=True)
                    best_name, best_score, fp, cycles, n = candidates_scores[0]
                    return best_name, f"data_driven:{cat} (sucesso={fp:.0%}, ciclos={cycles:.1f}, n={n})"
            except Exception as e:  # noqa: BLE001
                log.warning("Falha ao consultar estatísticas de roteamento: %s", e)

        if not registry:
            return default_agent, "default_agent"

        cap_reg: CapabilityRegistry | None = getattr(registry, "capability_registry", None)

        # Categorias de alto raciocínio exigem ModelCapability.REASONING
        if cat in cls.HIGH_REASONING_CATEGORIES:
            candidates = []
            for name in getattr(registry, "names", lambda: [])():
                ok, _ = registry.can_accept(name)
                if not ok:
                    continue
                state = registry.get(name)
                backend = state.backend
                model_id = getattr(backend, "model", "")
                has_reasoning = (
                    cap_reg.has_capability(model_id, ModelCapability.REASONING)
                    if cap_reg
                    else ("gemini" in name)
                )
                if has_reasoning:
                    candidates.append(name)
            if candidates:
                return candidates[0], f"intelligent_reasoning:{cat}"

        # Categorias de asset / boilerplate / docs / ui priorizam o menor custo de modelo
        if cat in cls.COST_EFFICIENT_CATEGORIES:
            candidates_with_cost = []
            for name in getattr(registry, "names", lambda: [])():
                ok, _ = registry.can_accept(name)
                if not ok:
                    continue
                state = registry.get(name)
                backend = state.backend
                model_id = getattr(backend, "model", "")
                spec = cap_reg.get_model(model_id) if cap_reg else None
                cost = (spec.cost_per_million_input + spec.cost_per_million_output) if spec else (0.27 if "deepseek" in name else 1.25)
                candidates_with_cost.append((name, cost))

            if candidates_with_cost:
                candidates_with_cost.sort(key=lambda x: x[1])
                cheapest_agent = candidates_with_cost[0][0]
                return cheapest_agent, f"intelligent_cost_efficiency:{cat}"

        # Se nenhum critério específico foi acionado, verifica disponibilidade do default
        ok, _ = registry.can_accept(default_agent)
        if ok:
            return default_agent, "default_agent"

        for name in getattr(registry, "names", lambda: [])():
            ok, _ = registry.can_accept(name)
            if ok:
                return name, f"intelligent_available_fallback:{name}"

        return default_agent, "default_agent"


@dataclass
class AssignmentRules:
    default_agent: str = "gemini"
    categories: dict[str, str] = field(default_factory=dict)
    path_rules: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> AssignmentRules:
        if not isinstance(data, dict):
            return cls()
        default = str(data.get("default_agent", "gemini")).lower()
        cats = {str(k).lower(): str(v).lower() for k, v in (data.get("categories") or {}).items() if str(v).lower() in AGENTS}
        rules = [r for r in (data.get("path_rules") or []) if isinstance(r, dict) and str(r.get("agent", "")).lower() in AGENTS
                 and r.get("pattern")]
        return cls(default if default in AGENTS else "gemini", cats, rules)

    @classmethod
    def load(cls, path: Path) -> AssignmentRules:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls.from_dict(data)

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

    def decide(self, category: str, files: list[str], registry: Any | None = None, repo: Any | None = None) -> tuple[str, str]:
        """Retorna (agente, regra aplicada)."""
        cat = (category or "").lower()
        if cat in self.categories:
            return self.categories[cat], f"category:{cat}"
        for rule in self.path_rules:
            for f in files or []:
                if self._match(f, rule["pattern"]):
                    return str(rule["agent"]).lower(), f"path:{rule['pattern']}"
        if registry or repo:
            return IntelligentRouter.route(cat, files, registry, self.default_agent, repo=repo)
        return self.default_agent, "default_agent"


def resolve_assignment(task: dict, proposed_by: str, response: AssignmentResponseItem | None, responder: str | None,
                       rules: AssignmentRules, gemini_unavailable_reason: str | None,
                       registry: Any | None = None, repo: Any | None = None) -> AssignmentDecision:
    """Decisão final de uma subtarefa. No máximo 2 turnos (proposta + resposta); o desempate é por regras/roteamento inteligente."""
    proposed = task.get("assigned_to") or task.get("agent") or "deepseek"
    base = {"task_id": task.get("id", ""), "proposed_by": proposed_by, "proposed_to": proposed,
            "proposal_reason": task.get("assignment_reason", ""), "responder": responder}
    if response is None:
        # Quando não há negociação (ex.: modo simples ou negociação desabilitada),
        # o histórico empírico comprovado (data-driven) decide se houver dados suficientes.
        if repo and hasattr(repo, "get_routing_stats"):
            dd_assigned, dd_rule = rules.decide(task.get("category", "other"), task.get("files", []), registry=registry, repo=repo)
            if "data_driven:" in dd_rule:
                assigned = dd_assigned
                decided_by = "data_driven"
                rule = dd_rule
                reason = f"Atribuição orientada a dados ({rule}) -> {assigned}."
            else:
                assigned, decided_by, rule, reason = proposed, "no_negotiation", "", "Sem resposta do outro agente; mantida a proposta."
        else:
            assigned, decided_by, rule, reason = proposed, "no_negotiation", "", "Sem resposta do outro agente; mantida a proposta."
        base["response"] = "none"
    elif response.decision == "accept" or response.proposed_agent == proposed:
        # Contraproposta que pede o mesmo agente proposto equivale a aceite.
        assigned, decided_by, rule, reason = proposed, "consensus", "", "Proposta aceita."
        base["response"] = "accept"
    else:
        assigned, rule = rules.decide(task.get("category", "other"), task.get("files", []), registry=registry, repo=repo)
        decided_by = "rules" if ("category:" in rule or "path:" in rule or "default" in rule) else ("data_driven" if "data_driven:" in rule else "intelligent_routing")
        reason = f"Contestação de {responder}; desempate por {rule} -> {assigned}."
        base.update(response="counter", counter_reason=response.reason)
    if assigned == "gemini" and gemini_unavailable_reason:
        reason = f"Gemini sem cota/indisponível ({gemini_unavailable_reason[:200]}); DeepSeek assume. Decisão anterior: {decided_by}."
        assigned, decided_by = "deepseek", "quota_fallback"
    return AssignmentDecision(**base, assigned_to=assigned, reviewer=other(assigned), decided_by=decided_by, rule=rule, reason=reason)
