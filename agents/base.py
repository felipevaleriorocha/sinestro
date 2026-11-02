"""Contrato comum dos backends de agente (Gemini/AGY, DeepSeek/OpenCode, mocks)."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import UTC
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


def parse_cooldown_seconds(message: str, default_seconds: int = 3600) -> int:
    """Extrai tempo de espera em segundos de mensagens de quota/rate-limit.
    Suporta ex.: 'reseta em 36h57m', '36h57m', 'retry-after: 120', 'in 15 minutes', etc.
    """
    if not message or not isinstance(message, str):
        return default_seconds
    text = message.lower()

    # 1. Padrão composto com prefixo de contexto
    compact = re.search(
        r"(?:reseta|reset|retry|wait|tente|tentativa|bloqueado)[^\d\n]*?"
        r"(?:(?:em|in|after|após)\s*)?"
        r"(?:(?P<days>\d+)\s*(?:d|dias?|days?)\s*)?"
        r"(?:(?P<hours>\d+)\s*(?:h|horas?|hours?)\s*)?"
        r"(?:(?P<mins>\d+)\s*(?:m|min|minutos?|minutes?)\s*)?"
        r"(?:(?P<secs>\d+)\s*(?:s|seg|segundos?|seconds?))?",
        text,
    )
    if compact and (compact.group("days") or compact.group("hours") or compact.group("mins") or compact.group("secs")):
        d = int(compact.group("days") or 0)
        h = int(compact.group("hours") or 0)
        m = int(compact.group("mins") or 0)
        s = int(compact.group("secs") or 0)
        total = d * 86400 + h * 3600 + m * 60 + s
        if total > 0:
            return total

    # 2. Padrão direto XhYm sem prefixo
    dhms = re.search(
        r"(?:(?P<days>\d+)\s*(?:d|dias?|days?)\s*)?"
        r"(?P<hours>\d+)\s*(?:h|horas?|hours?)\s*"
        r"(?:e\s*)?(?P<mins>\d+)?\s*(?:m|min|minutos?|minutes?)?",
        text,
    )
    if dhms and (dhms.group("hours") or dhms.group("days")):
        d = int(dhms.group("days") or 0)
        h = int(dhms.group("hours") or 0)
        m = int(dhms.group("mins") or 0)
        total = d * 86400 + h * 3600 + m * 60
        if total > 0:
            return total

    # 3. retry-after: NNN
    ra = re.search(r"retry[-_ ]after[:\s]+(\d+)", text)
    if ra:
        val = int(ra.group(1))
        if val > 0:
            return val

    # 4. Apenas minutos
    just_mins = re.search(r"(?:em|in|after|após)\s*(\d+)\s*(?:m|min|minutos?|minutes?)", text)
    if just_mins:
        val = int(just_mins.group(1))
        if val > 0:
            return val * 60

    # 5. Apenas segundos
    just_secs = re.search(r"(?:em|in|after|após)\s*(\d+)\s*(?:s|seg|segundos?|seconds?)", text)
    if just_secs:
        val = int(just_secs.group(1))
        if val > 0:
            return val

    return default_seconds


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
        seconds = retry_after if retry_after is not None else parse_cooldown_seconds(message, default_seconds=self.default_minutes * 60)
        until = datetime.now(UTC) + timedelta(seconds=seconds)
        state = self._read()
        state["quota"] = {
            "until": until.isoformat(),
            "seconds": seconds,
            "message": redact(message)[:500],
            "model": self.model,
        }
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
        now = datetime.now(UTC)
        if until > now:
            remaining_secs = int((until - now).total_seconds())
            hours, rem = divmod(remaining_secs, 3600)
            mins = rem // 60
            time_str = f"{hours}h{mins}m" if hours > 0 else f"{mins}m"
            return f"Quota do modelo {self.model} excedida; liberado em ~{time_str} ({until.isoformat()}). {quota.get('message', '')[:180]}"
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
