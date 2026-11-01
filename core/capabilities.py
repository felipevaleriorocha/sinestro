"""Definição de capacidades de modelos e registro central (CapabilityRegistry).

Prepara o sistema para suporte a capacidades heterogêneas de modelos/provedores
(raciocínio profundo, visão computacional, geração de código, chamadas de ferramentas, etc.),
servindo de base para o roteamento inteligente e execução multi-provider.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

log = logging.getLogger("core.capabilities")


class ModelCapability(str, Enum):
    """Capacidades suportadas por modelos de linguagem e agentes."""
    TEXT = "text"
    CODE = "code"
    REASONING = "reasoning"
    VISION = "vision"
    TOOL_CALLING = "tool_calling"
    READ_ONLY = "read_only"
    STREAMING = "streaming"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True)
class ModelSpec:
    """Especificação e metadados de um modelo suportado por um provedor."""
    id: str
    name: str
    provider_id: str
    capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.TEXT, ModelCapability.CODE})
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    description: str = ""

    def supports(self, cap: ModelCapability | str) -> bool:
        """Verifica se o modelo suporta a capacidade solicitada."""
        if isinstance(cap, str):
            try:
                cap = ModelCapability(cap.lower())
            except ValueError:
                return False
        return cap in self.capabilities


class CapabilityRegistry:
    """Registro thread-safe de provedores, modelos e suas respectivas capacidades."""

    def __init__(self) -> None:
        self._models: dict[str, ModelSpec] = {}
        self._providers: dict[str, Any] = {}

    def register_provider(self, provider: Any) -> None:
        """Registra um provedor e seus modelos associados."""
        self._providers[provider.id] = provider
        if hasattr(provider, "get_models"):
            for model in provider.get_models():
                self.register_model(model)

    def register_model(self, model: ModelSpec) -> None:
        """Registra uma especificação de modelo."""
        self._models[model.id] = model

    def get_model(self, model_id: str) -> ModelSpec | None:
        """Recupera especificação de um modelo pelo id."""
        return self._models.get(model_id)

    def get_provider(self, provider_id: str) -> Any | None:
        """Recupera provedor pelo id."""
        return self._providers.get(provider_id)

    def list_models(self) -> list[ModelSpec]:
        """Lista todos os modelos registrados."""
        return list(self._models.values())

    def list_providers(self) -> list[Any]:
        """Lista todos os provedores registrados."""
        return list(self._providers.values())

    def find_models(self, capability: ModelCapability | str) -> list[ModelSpec]:
        """Retorna todos os modelos que suportam a capacidade informada."""
        return [m for m in self._models.values() if m.supports(capability)]

    def has_capability(self, model_id: str, capability: ModelCapability | str) -> bool:
        """Verifica se um determinado modelo possui a capacidade solicitada."""
        m = self.get_model(model_id)
        return m.supports(capability) if m else False
