"""Contrato comum dos backends de agente (Gemini/AGY, DeepSeek/OpenCode, mocks)."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from core.agent import Agent, AgentDefinition, SkillDef
from core.capabilities import CapabilityRegistry, ModelCapability, ModelSpec
from core.providers import (
    AgentAuthError,
    AgentCancelledError,
    AgentConnectionError,
    AgentError,
    AgentHealth,
    AgentPermissionError,
    AgentResponseError,
    AgentResult,
    AgentTimeoutError,
    AgentUnavailableError,
    ContractNotVerifiedError,
    ModelUnavailableError,
    Provider,
    ProviderError,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailableError,
    QuotaExceededError,
)

# Skills que não devem editar arquivos: o adaptador usa modo/agente somente leitura quando disponível.
READ_ONLY_SKILLS = frozenset({"plan_feature", "respond_assignment", "review_implementation", "final_review"})


class QuotaCooldown:
    """Cooldown de quota persistido em arquivo (sobrevive a reinícios). Comum aos dois adaptadores."""

    def __init__(self, state_file, model: str, default_minutes: int):
        from pathlib import Path
        self.state_file = Path(state_file)
        self.model = model
        self.default_minutes = default_minutes

    def _read(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark(self, message: str, retry_after: int | None = None) -> None:
        from datetime import datetime, timedelta, timezone
        from core.logging_setup import redact
        seconds = retry_after if retry_after else self.default_minutes * 60
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        state = self._read()
        state["quota"] = {"until": until.isoformat(), "message": redact(message)[:500], "model": self.model}
        self._write(state)

    def clear(self) -> None:
        state = self._read()
        state.pop("quota", None)
        self._write(state)

    def reason(self) -> str | None:
        from datetime import datetime, timezone
        quota = self._read().get("quota")
        if not quota or quota.get("model") != self.model:
            return None
        try:
            until = datetime.fromisoformat(quota["until"])
        except (KeyError, ValueError):
            return None
        if until > datetime.now(timezone.utc):
            return f"Quota do modelo {self.model} excedida; nova tentativa após {until.isoformat()} ({quota.get('message', '')[:200]})."
        return None


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


# Registra Agent como subclasse virtual de AgentBackend
AgentBackend.register(Agent)


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
