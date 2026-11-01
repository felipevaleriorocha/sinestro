"""Registro de servidores MCP por agente (preparação para Roblox Studio MCP).

Não assume que os agentes estão conectados. Para cada servidor e agente informa:
  disabled        integração desligada na configuração
  not_configured  servidor habilitado mas agente sem acesso configurado
  reachable       verificação de saúde passou (processo/porta/URL ativos)
  unreachable     verificação falhou
  unknown         não há como verificar (health_check none) — tratar como não conectado
Operações via MCP devem reservar recursos `mcp:<servidor>:<alvo>` no FileLockManager.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from core.safe_exec import CommandNotAllowed, process_running


class MCPServerRegistry:
    def __init__(self, config_file: Path):
        self.config_file = Path(config_file)

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.config_file.read_text(encoding="utf-8")).get("servers", {})
        except (OSError, ValueError):
            return {}

    def _check(self, hc: dict) -> tuple[str, str]:
        kind = (hc or {}).get("type", "none")
        target = str((hc or {}).get("target", ""))
        try:
            if kind == "process":
                running = process_running(target)
                if running is None:
                    return "unknown", "verificação de processo indisponível neste SO"
                return ("reachable", f"processo {target} ativo") if running else ("unreachable", f"processo {target} não encontrado")
            if kind == "tcp":
                host, _, port = target.rpartition(":")
                with socket.create_connection((host or "127.0.0.1", int(port)), timeout=3):
                    return "reachable", f"porta {target} aberta"
            if kind == "http":
                if urlparse(target).hostname not in {"127.0.0.1", "localhost", "::1"}:
                    return "unknown", "somente URLs locais são verificadas"
                r = httpx.get(target, timeout=5)
                return ("reachable" if r.status_code < 500 else "unreachable"), f"HTTP {r.status_code}"
        except (OSError, ValueError, httpx.HTTPError, CommandNotAllowed) as exc:
            return "unreachable", str(exc)[:200]
        return "unknown", "sem verificação configurada"

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for sid, cfg in self.load().items():
            enabled = bool(cfg.get("enabled"))
            server_state, detail = ("disabled", "integração desligada") if not enabled else self._check(cfg.get("health_check", {}))
            agents = {}
            for agent, acfg in (cfg.get("agents") or {}).items():
                if not enabled:
                    state = "disabled"
                elif not acfg.get("enabled"):
                    state = "not_configured"
                else:
                    state = server_state
                agents[agent] = {"state": state, "role": acfg.get("role"), "configured_in": acfg.get("configured_in")}
            out[sid] = {"description": cfg.get("description", ""), "enabled": enabled, "server_state": server_state,
                        "detail": detail, "agents": agents, "lock_namespace": cfg.get("lock_namespace", f"mcp:{sid}")}
        return out

    def agent_has_access(self, server_id: str, agent: str) -> bool:
        return self.status().get(server_id, {}).get("agents", {}).get(agent, {}).get("state") == "reachable"
