"""Identidades, capacidades (skills) e estado dos agentes; seleção do agente por subtarefa."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agents.base import AgentBackend, AgentHealth
from core.agent import Agent, AgentDefinition, SkillDef
from core.capabilities import CapabilityRegistry, ModelCapability

SKILL_PLAN = SkillDef("plan_feature", "Planejar funcionalidade",
                      "Analisa o pedido e o contexto do projeto e devolve um plano JSON com subtarefas, arquivos, critérios, "
                      "restrições e uma PROPOSTA de atribuição (assigned_to + motivo) por subtarefa.",
                      ("planning", "architecture"), ("Planeje o sistema de evolução AFK do Mundo 1",))
SKILL_ASSIGN = SkillDef("respond_assignment", "Responder atribuição",
                        "Aceita ou contesta (com motivo) a atribuição proposta para cada subtarefa, em um único turno.",
                        ("planning", "negotiation"))
SKILL_IMPLEMENT = SkillDef("implement_task", "Implementar subtarefa",
                           "Implementa/corrige código e funcionalidades conforme objetivo, contexto, arquivos, critérios e restrições; "
                           "devolve resumo, arquivos modificados, decisões técnicas, problemas e resultado.",
                           ("implementation", "bugfix", "refactor"))
SKILL_REVIEW = SkillDef("review_implementation", "Revisar implementação do outro agente",
                        "Revisa, em modo somente leitura, o trabalho do OUTRO agente com checklist (critérios de aceitação, APIs do domínio, "
                        "casos de borda, segurança cliente/servidor) e achados com evidência (arquivo, linha e trecho).",
                        ("review", "quality"))
SKILL_FINAL = SkillDef("final_review", "Revisão final", "Avalia o resultado consolidado do workflow.", ("review",))

SYMMETRIC_SKILLS = (SKILL_PLAN, SKILL_ASSIGN, SKILL_IMPLEMENT, SKILL_REVIEW, SKILL_FINAL)

AGENT_DEFINITIONS: dict[str, AgentDefinition] = {
    "gemini": AgentDefinition(
        name="gemini",
        display_name="Gemini — Tech Lead",
        role="CTO / Tech Lead",
        description="Mais forte em arquitetura, segurança, lógica de backend/servidor e interfaces principais. Planeja, implementa e revisa; "
                    "nunca revisa o próprio trabalho.",
        skills=SYMMETRIC_SKILLS,
        provider="Google (via Antigravity CLI)",
    ),
    "deepseek": AgentDefinition(
        name="deepseek",
        display_name="DeepSeek — Senior Developer",
        role="Senior Developer",
        description="Mais forte em implementação, UI, lógica detalhada, boilerplate e configuração. Planeja, implementa e revisa; "
                    "nunca revisa o próprio trabalho.",
        skills=SYMMETRIC_SKILLS,
        provider="DeepSeek (via OpenCode)",
    ),
}


@dataclass
class AgentState:
    definition: AgentDefinition
    backend: AgentBackend
    health: AgentHealth | None = None
    health_at: float = 0.0
    card_reachable: bool | None = None
    card_error: str | None = None
    extra: dict = field(default_factory=dict)


class AgentRegistry:
    def __init__(
        self,
        backends: dict[str, AgentBackend],
        definitions: dict[str, AgentDefinition] | None = None,
        allow_fallback: bool = False,
        health_ttl: float = 30.0,
        capability_registry: CapabilityRegistry | None = None,
    ):
        definitions = definitions or AGENT_DEFINITIONS
        self.agents: dict[str, AgentState] = {}
        for n, b in backends.items():
            defn = definitions.get(n) or (b.definition if isinstance(b, Agent) else AgentDefinition(
                name=n, display_name=n, role="Agent", description="", skills=(), provider=getattr(b, "kind", "unknown")
            ))
            self.agents[n] = AgentState(defn, b)
        self.allow_fallback = allow_fallback
        self.health_ttl = health_ttl
        self.capability_registry = capability_registry

    def names(self) -> list[str]:
        return list(self.agents)

    def get(self, name: str) -> AgentState:
        if name not in self.agents:
            raise KeyError(f"Agente desconhecido: {name}")
        return self.agents[name]

    async def check(self, name: str, deep: bool = False) -> AgentHealth:
        st = self.get(name)
        if not deep and st.health and time.time() - st.health_at < self.health_ttl:
            return st.health
        st.health = await asyncio.to_thread(st.backend.health, deep)
        st.health_at = time.time()
        return st.health

    def can_accept(self, name: str) -> tuple[bool, str | None]:
        """Decisão rápida (sem rede): o backend recusa trabalho? (ex.: quota em cooldown)."""
        reason = self.get(name).backend.unavailable_reason()
        return (reason is None), reason

    def select(
        self,
        preferred: str,
        skill: str,
        required_capabilities: frozenset[ModelCapability] | set[ModelCapability] | None = None,
    ) -> tuple[str | None, str | None]:
        """Retorna (agente, motivo_de_bloqueio). Sem fallback, não executa se o preferido estiver indisponível."""
        candidates = [preferred] + ([n for n in self.agents if n != preferred] if self.allow_fallback else [])
        reasons = []
        for name in candidates:
            if name not in self.agents or skill not in self.agents[name].definition.skill_ids():
                continue
            # Verificação de capacidades do modelo/provedor se exigidas
            if required_capabilities:
                backend = self.agents[name].backend
                missing = []
                for cap in required_capabilities:
                    if hasattr(backend, "supports_capability") and not backend.supports_capability(cap):
                        missing.append(cap.value if hasattr(cap, "value") else str(cap))
                    elif self.capability_registry and not self.capability_registry.has_capability(backend.model, cap):
                        missing.append(cap.value if hasattr(cap, "value") else str(cap))
                if missing:
                    reasons.append(f"{name}: faltam capacidades ({', '.join(missing)})")
                    continue
            ok, reason = self.can_accept(name)
            if ok:
                return name, None
            reasons.append(f"{name}: {reason}")
        return None, "; ".join(reasons) or f"Nenhum agente com a skill {skill}."

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for name, st in self.agents.items():
            ok, reason = self.can_accept(name)
            entry: dict[str, Any] = {
                "name": name,
                "display_name": st.definition.display_name,
                "role": st.definition.role,
                "model": st.backend.model,
                "backend": st.backend.kind,
                "skills": sorted(st.definition.skill_ids()),
                "accepting_work": ok,
                "blocked_reason": reason,
                "health": st.health.to_dict() if st.health else None,
                "a2a_card_reachable": st.card_reachable,
                "a2a_card_error": st.card_error,
            }
            if isinstance(st.backend, Agent):
                entry["required_capabilities"] = [c.value for c in st.backend.required_capabilities]
            out.append(entry)
        return out
