"""Entidade de Agente (Persona + Skills + Associação a Model/Provider).

Representa um agente autônomo desacoplado do mecanismo de execução de baixo nível,
combinando persona (papel e descrição), habilidades (skills) e vinculação a um
provedor (Provider) e modelo (Model).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.capabilities import ModelCapability
from core.providers import AgentHealth, AgentResult, Provider

log = logging.getLogger("core.agent")


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

    def to_agent(self, provider: Provider, model: str | None = None) -> Agent:
        return Agent(
            name=self.name,
            display_name=self.display_name,
            role=self.role,
            description=self.description,
            skills=self.skills,
            provider=provider,
            model=model,
        )


class Agent:
    """Entidade que combina Persona, Habilidades (Skills) e Provedor/Modelo."""

    def __init__(
        self,
        name: str,
        display_name: str,
        role: str,
        description: str,
        skills: tuple[SkillDef, ...] | list[SkillDef],
        provider: Provider,
        model: str | None = None,
        required_capabilities: frozenset[ModelCapability] | set[ModelCapability] = frozenset(),
        extra: dict[str, Any] | None = None,
    ):
        self.name = name
        self.display_name = display_name
        self.role = role
        self.description = description
        self.skills = tuple(skills)
        self.provider = provider
        self.model_name = model or provider.default_model
        self.required_capabilities = frozenset(required_capabilities)
        self.extra = extra or {}

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def kind(self) -> str:
        return getattr(self.provider, "kind", getattr(self.provider, "id", "agent"))

    def skill_ids(self) -> set[str]:
        return {s.id for s in self.skills}

    def supports_skill(self, skill_id: str) -> bool:
        return skill_id in self.skill_ids()

    def supports_capability(self, capability: ModelCapability | str) -> bool:
        return self.provider.supports_capability(capability, self.model_name)

    def run(
        self,
        prompt: str,
        *,
        context_key: str,
        run_id: str,
        options: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Executa a solicitação delegando ao Provider com contexto de persona."""
        opts = dict(options or {})
        opts.setdefault("role", self.role)
        opts.setdefault("persona", self.description)
        opts.setdefault("agent_name", self.name)
        return self.provider.execute(
            prompt,
            model=self.model_name,
            context_key=context_key,
            run_id=run_id,
            options=opts,
        )

    def health(self, deep: bool = False) -> AgentHealth:
        """Consulta saúde via provedor retornando identidade do agente."""
        h = self.provider.health(deep=deep)
        return AgentHealth(
            name=self.name,
            available=h.available,
            status=h.status,
            detail=h.detail,
            model=self.model_name,
            checks=h.checks,
            checked_at=h.checked_at,
        )

    def cancel(self, run_id: str) -> bool:
        """Delega cancelamento ao provedor."""
        return self.provider.cancel(run_id)

    def unavailable_reason(self) -> str | None:
        """Consulta indisponibilidade no provedor para este modelo."""
        return self.provider.unavailable_reason(self.model_name)

    @property
    def definition(self) -> AgentDefinition:
        """Gera a definição equivalente para compatibilidade com esquemas existentes."""
        return AgentDefinition(
            name=self.name,
            display_name=self.display_name,
            role=self.role,
            description=self.description,
            skills=self.skills,
            provider=self.provider.name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "role": self.role,
            "description": self.description,
            "model": self.model_name,
            "provider": self.provider.name,
            "skills": sorted(self.skill_ids()),
            "required_capabilities": [c.value for c in self.required_capabilities],
        }
