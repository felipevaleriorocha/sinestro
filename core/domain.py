"""Interface base do perfil de domínio (DomainProfile).

Desacopla regras de negócio, prompts, checagens e fontes de contexto específicas
do domínio (ex: Roblox/Luau, Web/React, etc.) do núcleo do orquestrador.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DomainProfile(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Nome identificador do domínio (ex: 'roblox')."""
        ...

    @abstractmethod
    def get_prompts(self) -> Any:
        """Retorna o módulo ou gerador de prompts do domínio."""
        ...

    @abstractmethod
    def load_assignment_rules(self) -> dict[str, Any]:
        """Retorna as regras de atribuição de tarefas do domínio já analisadas (parsed)."""
        ...

    @abstractmethod
    def load_checks_config(self) -> dict[str, Any]:
        """Retorna a configuração de checagens do domínio já analisada (parsed)."""
        ...

    @abstractmethod
    def load_context_sources(self) -> dict[str, Any]:
        """Retorna as fontes de contexto do domínio já analisadas (parsed)."""
        ...

    def get_agent_definitions(self) -> dict[str, Any] | None:
        """Opcional: definições de agentes especializadas para o domínio."""
        return None
