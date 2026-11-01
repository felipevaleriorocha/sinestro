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
    cost_per_million_input: float = 0.0
    cost_per_million_output: float = 0.0
    cost_tier: str = "standard"  # "low", "standard", "high"

    def supports(self, cap: ModelCapability | str) -> bool:
        """Verifica se o modelo suporta a capacidade solicitada."""
        if isinstance(cap, str):
            try:
                cap = ModelCapability(cap.lower())
            except ValueError:
                return False
        return cap in self.capabilities

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calcula o custo estimado em dólares para a contagem de tokens informada."""
        return (input_tokens * self.cost_per_million_input + output_tokens * self.cost_per_million_output) / 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        """Serializa metadados do modelo para inspeção e API."""
        return {
            "id": self.id,
            "name": self.name,
            "provider_id": self.provider_id,
            "capabilities": sorted(c.value for c in self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "description": self.description,
            "cost_per_million_input": self.cost_per_million_input,
            "cost_per_million_output": self.cost_per_million_output,
            "cost_tier": self.cost_tier,
        }


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

    def find_optimal_model(
        self,
        required_capabilities: set[ModelCapability] | frozenset[ModelCapability],
        prefer_cost_efficiency: bool = True,
    ) -> ModelSpec | None:
        """Encontra o modelo ótimo que satisfaz todas as capacidades requeridas."""
        matching = [
            m for m in self._models.values()
            if all(m.supports(cap) for cap in required_capabilities)
        ]
        if not matching:
            return None

        if prefer_cost_efficiency:
            matching.sort(key=lambda m: (m.cost_per_million_input + m.cost_per_million_output, -m.context_window))
        else:
            matching.sort(key=lambda m: (-len(m.capabilities), m.cost_per_million_input))
        return matching[0]

    def estimate_cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        """Estima o custo em dólares para um modelo específico."""
        m = self.get_model(model_id)
        return m.estimate_cost(input_tokens, output_tokens) if m else 0.0

    def describe(self) -> list[dict[str, Any]]:
        """Retorna especificações de todos os modelos serializadas."""
        return [m.to_dict() for m in self._models.values()]
