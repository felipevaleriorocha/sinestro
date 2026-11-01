"""Identidades, capacidades (skills) e estado dos agentes; seleção do agente por subtarefa."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from agents.base import AgentBackend, AgentHealth


@dataclass(frozen=True)
class SkillDef:
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    display_name: str
    role: str
    description: str
    skills: tuple[SkillDef, ...]
    provider: str

    def skill_ids(self) -> set[str]:
        return {s.id for s in self.skills}


SKILL_PLAN = SkillDef("plan_feature", "Planejar funcionalidade",
                      "Analisa o pedido e o contexto do projeto e devolve um plano JSON com subtarefas, arquivos, critérios e restrições.",
                      ("planning", "architecture"), ("Planeje o sistema de evolução AFK do Mundo 1",))
SKILL_REVIEW = SkillDef("review_implementation", "Revisar implementação",
                        "Revisa o relatório e os arquivos alterados; aprova, solicita ajustes, questiona decisões ou rejeita com justificativa.",
                        ("review", "quality"))
SKILL_FINAL = SkillDef("final_review", "Revisão final", "Avalia o resultado consolidado do workflow.", ("review",))
SKILL_IMPLEMENT = SkillDef("implement_task", "Implementar subtarefa",
                           "Implementa/corrige scripts Luau e mecânicas de RPG conforme objetivo, contexto, arquivos, critérios e restrições; "
                           "devolve resumo, arquivos modificados, decisões técnicas, problemas e resultado.",
                           ("luau", "roblox", "rpg", "bugfix", "refactor"))

AGENT_DEFINITIONS: dict[str, AgentDefinition] = {
    "gemini": AgentDefinition(
        name="gemini",
        display_name="Gemini 4 Argon High — Tech Lead",
        role="CTO / Tech Lead",
        description="Planeja funcionalidades, analisa arquitetura, distribui tarefas, define contratos, revisa código e solicita correções.",
        skills=(SKILL_PLAN, SKILL_REVIEW, SKILL_FINAL, SKILL_IMPLEMENT),
        provider="Google (via Antigravity CLI)",
    ),
    "deepseek": AgentDefinition(
        name="deepseek",
        display_name="DeepSeek V4.1 Flash Free — Senior Developer",
        role="Senior Developer",
        description="Implementa sistemas Luau e mecânicas de RPG, cria e corrige scripts, resolve bugs e refatora código.",
        skills=(SKILL_IMPLEMENT,),
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
    def __init__(self, backends: dict[str, AgentBackend], definitions: dict[str, AgentDefinition] | None = None,
                 allow_fallback: bool = False, health_ttl: float = 30.0):
        definitions = definitions or AGENT_DEFINITIONS
        self.agents: dict[str, AgentState] = {n: AgentState(definitions[n], b) for n, b in backends.items()}
        self.allow_fallback = allow_fallback
        self.health_ttl = health_ttl

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

    def select(self, preferred: str, skill: str) -> tuple[str | None, str | None]:
        """Retorna (agente, motivo_de_bloqueio). Sem fallback, não executa se o preferido estiver indisponível."""
        candidates = [preferred] + ([n for n in self.agents if n != preferred] if self.allow_fallback else [])
        reasons = []
        for name in candidates:
            if name not in self.agents or skill not in self.agents[name].definition.skill_ids():
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
            out.append({
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
            })
        return out
