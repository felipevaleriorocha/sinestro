"""Contrato comum dos backends de agente (Gemini/AGY, DeepSeek/OpenCode, mocks)."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------- erros tipados
class AgentError(RuntimeError):
    code = "agent_error"
    retryable = False


class AgentUnavailableError(AgentError):
    """Servidor/CLI fora do ar ou modelo indisponível. Não executar a tarefa."""
    code = "agent_unavailable"
    retryable = True


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


# ---------------------------------------------------------------- resultados
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


@dataclass
class AgentHealth:
    name: str
    available: bool
    status: str  # ok | degraded | unavailable | quota_exceeded | not_configured | unknown
    detail: str = ""
    model: str | None = None
    checks: dict = field(default_factory=dict)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "name": self.name, "available": self.available, "status": self.status, "detail": self.detail,
            "model": self.model, "checks": self.checks, "checked_at": self.checked_at,
        }


class AgentBackend(ABC):
    """Backend que efetivamente executa o modelo. O A2A fica na camada `communication`."""

    name: str = "agent"
    kind: str = "base"

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        """Executa um prompt (bloqueante). `context_key` agrupa a conversa; `run_id` permite cancelar."""

    @abstractmethod
    def health(self, deep: bool = False) -> AgentHealth:
        """Verificação sem consumir tokens."""

    def cancel(self, run_id: str) -> bool:
        return False

    def unavailable_reason(self) -> str | None:
        """Motivo para recusar trabalho antes de executar (ex.: quota em cooldown). None = pode executar."""
        return None


# ---------------------------------------------------------------- utilitários
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Extrai um objeto JSON da resposta do modelo (puro, cercado por ``` ou embutido em texto)."""
    if not isinstance(text, str):
        raise AgentResponseError("Resposta não textual.")
    stripped = text.strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for match in _FENCE_RE.finditer(stripped):
        try:
            value = json.loads(match.group(1))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    # Varre objetos balanceados do maior para o menor.
    starts = [i for i, ch in enumerate(stripped) if ch == "{"]
    decoder = json.JSONDecoder()
    best: dict | None = None
    for s in starts:
        try:
            value, _ = decoder.raw_decode(stripped[s:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (best is None or len(json.dumps(value)) > len(json.dumps(best))):
            best = value
    if best is not None:
        return best
    raise AgentResponseError("O agente não retornou um objeto JSON válido.")


QUOTA_PATTERNS = re.compile(
    r"(quota|rate[ _-]?limit|too many requests|insufficient[_ ](?:quota|credits|balance)|"
    r"free (?:tier|usage|limit)|limit (?:exceeded|reached)|exceeded your|resource[_ ]exhausted|credits? (?:exhausted|depleted)|429)",
    re.IGNORECASE,
)


def looks_like_quota_error(text: str) -> bool:
    return bool(text) and bool(QUOTA_PATTERNS.search(text))
