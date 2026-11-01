"""Implementação do DomainProfile para o domínio Roblox/Luau."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.domain import DomainProfile
from domains.roblox import prompts

DEFAULT_CONFIG_DIR = Path(__file__).parent / "config"


class RobloxDomainProfile(DomainProfile):
    """Perfil passivo do domínio Roblox/Luau.

    Carrega regras, prompts e configurações específicas de Roblox e entrega
    ao Core já analisadas (parsed).
    """

    def __init__(self, settings: Any | None = None, config_dir: Path | None = None):
        self.settings = settings
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR

    @property
    def name(self) -> str:
        return "roblox"

    def get_prompts(self) -> Any:
        return prompts

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def load_assignment_rules(self) -> dict[str, Any]:
        path = getattr(self.settings, "assignment_rules_file", None) if self.settings else None
        if path:
            p = Path(path)
            if p.is_file():
                return self._read_json(p)
            if p.name != "assignment_rules.json":
                return {}
        fallback = self.config_dir / "assignment_rules.json"
        return self._read_json(fallback)

    def load_checks_config(self) -> dict[str, Any]:
        path = getattr(self.settings, "checks_file", None) if self.settings else None
        if path:
            p = Path(path)
            if p.is_file():
                data = self._read_json(p)
                if "allowed_tools" not in data:
                    data["allowed_tools"] = ["selene", "stylua", "luau-analyze", "luau-lsp"]
                return data
            if p.name != "checks.json":
                return {"checks": [], "allowed_tools": ["selene", "stylua", "luau-analyze", "luau-lsp"]}
        fallback = self.config_dir / "checks.json"
        data = self._read_json(fallback)
        if "allowed_tools" not in data:
            data["allowed_tools"] = ["selene", "stylua", "luau-analyze", "luau-lsp"]
        return data

    def load_context_sources(self) -> dict[str, Any]:
        path = getattr(self.settings, "context_sources_file", None) if self.settings else None
        if path:
            p = Path(path)
            if p.is_file():
                return self._read_json(p)
            if p.name != "context_sources.json":
                return {}
        fallback = self.config_dir / "context_sources.json"
        return self._read_json(fallback)
