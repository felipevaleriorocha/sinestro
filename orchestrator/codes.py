"""Códigos de erro A2A que indicam agente/modelo indisponível (a etapa pode ser pulada ou bloqueada, não falhada)."""
from __future__ import annotations

UNAVAILABLE_CODES = {"quota_exceeded", "agent_unavailable", "connection_failed", "model_unavailable", "contract_not_verified"}
