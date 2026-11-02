"""Interface base de Provedores de IA (Provider).

O Provider lida estritamente com a camada de comunicação (rede, SDK, CLI, processos locais),
gestão de cotas, limites, checagens de disponibilidade (health) e cancelamento.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.capabilities import ModelCapability, ModelSpec

log = logging.getLogger("core.providers")


# ---------------------------------------------------------------- erros tipados
class AgentError(RuntimeError):
    code = "agent_error"
    retryable = False


ProviderError = AgentError


class AgentUnavailableError(AgentError):
    """Servidor/CLI fora do ar ou modelo indisponível. Não executar a tarefa."""
    code = "agent_unavailable"
    retryable = True


ProviderUnavailableError = AgentUnavailableError


class AgentConnectionError(AgentUnavailableError):
    code = "connection_failed"


class ModelUnavailableError(AgentUnavailableError):
    code = "model_unavailable"


class QuotaExceededError(AgentUnavailableError):
    code = "quota_exceeded"

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AgentTimeoutError(AgentError):
    code = "timeout"
    retryable = True


class AgentAuthError(AgentError):
    code = "auth_failed"


class AgentResponseError(AgentError):
    code = "invalid_response"


class ContractNotVerifiedError(AgentUnavailableError):
    """Rota da API ainda não confirmada para a versão instalada; não chutar endpoints."""
    code = "contract_not_verified"


class AgentCancelledError(AgentError):
    code = "cancelled"


class AgentPermissionError(AgentError):
    """Ação requer aprovação/permissão não interativa."""
    code = "permission_denied"
    retryable = False

    def __init__(self, message: str, denied_actions: list[Any] | None = None):
        super().__init__(message)
        self.denied_actions = denied_actions or []


# ---------------------------------------------------------------- resultados e saúde
@dataclass
class AgentResult:
    text: str
    model: str | None = None
    session_id: str | None = None
    usage: dict | None = None
    duration_seconds: float | None = None
    num_turns: int | None = None
    raw_status: str | None = None
    extra: dict = field(default_factory=dict)


ProviderResult = AgentResult


@dataclass
class AgentHealth:
    name: str
    available: bool
    status: str  # ok | degraded | unavailable | quota_exceeded | not_configured | unknown
    detail: str = ""
    model: str | None = None
    checks: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "status": self.status,
            "detail": self.detail,
            "model": self.model,
            "checks": self.checks,
            "checked_at": self.checked_at,
        }


ProviderHealth = AgentHealth


# ---------------------------------------------------------------- contrato Provider
class Provider(ABC):
    """Contrato abstrato de um Provedor de IA.

    Responsável exclusivamente pela comunicação externa (HTTP, SDK, CLI, processo),
    verificação de integridade, gestão de cotas/limites e cancelamento.
    """

    id: str = "provider"
    name: str = "Base Provider"
    description: str = ""
    kind: str = "base"

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Modelo padrão associado a este provedor."""
        ...

    @abstractmethod
    def get_models(self) -> list[ModelSpec]:
        """Retorna lista de especificações dos modelos suportados."""
        ...

    @abstractmethod
    def execute(
        self,
        prompt: str,
        *,
        model: str | None = None,
        context_key: str = "",
        run_id: str = "",
        options: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Executa uma solicitação ao modelo, gerenciando cotas, rede e limites."""
        ...

    @abstractmethod
    def health(self, deep: bool = False) -> AgentHealth:
        """Verifica a saúde do provedor sem consumir cotas/tokens desnecessários."""
        ...

    def cancel(self, run_id: str) -> bool:
        """Cancela uma execução em andamento identificada por run_id."""
        return False

    def unavailable_reason(self, model: str | None = None) -> str | None:
        """Motivo pelo qual o provedor não pode aceitar trabalho (ex: cooldown de cota)."""
        return None

    def supports_capability(self, capability: ModelCapability | str, model: str | None = None) -> bool:
        """Verifica se o provedor e seu modelo suportam a capacidade informada."""
        target_model = model or self.default_model
        for m in self.get_models():
            if m.id == target_model:
                return m.supports(capability)
        return False
