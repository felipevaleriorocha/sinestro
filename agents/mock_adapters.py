"""Backends SIMULADOS para testes e demonstração (ORCH_MOCK_AGENTS=true).

Nada aqui prova integração real com os modelos. As respostas seguem os contratos JSON
para exercitar o fluxo A2A completo com papéis simétricos (planejar, atribuir, implementar, revisar).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from agents.base import AgentBackend, AgentCancelledError, AgentHealth, AgentResult, QuotaExceededError
from core.capabilities import ModelCapability, ModelSpec
from core.providers import Provider

Responder = Callable[[str, str, dict], str]


class MockProvider(Provider):
    """Provedor simulado para testes rápidos e demonstração sem consumo de tokens reais."""

    id = "mock"
    name = "Mock Provider"
    kind = "mock"
    description = "Provedor simulado para testes unitários e de integração"

    def __init__(
        self,
        name: str,
        model: str,
        responder: Responder | None = None,
        delay: float = 0.0,
        capabilities: frozenset[ModelCapability] | None = None,
        cost_per_million_input: float | None = None,
        cost_per_million_output: float | None = None,
        cost_tier: str | None = None,
    ):
        self.id = f"mock-{name}"
        self.name = name
        self._model = model
        self.responder = responder or (lambda skill, prompt, calls: json.dumps({"summary": "ok"}))
        self.delay = delay
        self.calls: list[dict] = []
        self.quota_reason: str | None = None
        self.raise_on_run: Exception | None = None
        self.raise_on_skill: dict[str, Exception] = {}
        self._cancel: dict[str, threading.Event] = {}

        is_deepseek = "deepseek" in name.lower()
        if capabilities is not None:
            self._capabilities = capabilities
        elif is_deepseek:
            self._capabilities = frozenset({ModelCapability.TEXT, ModelCapability.CODE, ModelCapability.READ_ONLY})
        else:
            self._capabilities = frozenset({
                ModelCapability.TEXT,
                ModelCapability.CODE,
                ModelCapability.REASONING,
                ModelCapability.READ_ONLY,
                ModelCapability.VISION,
            })

        self._cost_in = cost_per_million_input if cost_per_million_input is not None else (0.27 if is_deepseek else 1.25)
        self._cost_out = cost_per_million_output if cost_per_million_output is not None else (1.10 if is_deepseek else 5.00)
        self._cost_tier = cost_tier or ("low" if is_deepseek else "high")

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def model(self) -> str:
        return self._model

    def get_models(self) -> list[ModelSpec]:
        return [
            ModelSpec(
                id=self._model,
                name=f"{self.name} Mock Model",
                provider_id=self.id,
                capabilities=self._capabilities,
                context_window=64_000 if "deepseek" in self.name.lower() else 1_000_000,
                cost_per_million_input=self._cost_in,
                cost_per_million_output=self._cost_out,
                cost_tier=self._cost_tier,
                description="Modelo simulado para testes.",
            )
        ]

    def skill_calls(self, skill: str) -> list[dict]:
        return [c for c in self.calls if c["skill"] == skill]

    def execute(
        self,
        prompt: str,
        *,
        model: str | None = None,
        context_key: str = "",
        run_id: str = "",
        options: dict[str, Any] | None = None,
    ) -> AgentResult:
        options = options or {}
        skill = options.get("skill", "")
        self.calls.append({
            "skill": skill,
            "context_key": context_key,
            "run_id": run_id,
            "prompt": prompt,
            "read_only": options.get("read_only"),
        })
        if self.quota_reason:
            raise QuotaExceededError(self.quota_reason)
        if self.raise_on_run:
            raise self.raise_on_run
        if skill in self.raise_on_skill:
            raise self.raise_on_skill[skill]
        ev = self._cancel.setdefault(run_id, threading.Event())
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if ev.wait(0.02):
                raise AgentCancelledError("cancelado (mock)")
        text = self.responder(skill, prompt, {"count": len(self.skill_calls(skill))})
        return AgentResult(
            text=text,
            model=model or self.model,
            session_id=f"mock-{context_key[:8]}",
            usage={"input_tokens": 1, "output_tokens": 1},
            duration_seconds=round(self.delay, 3),
        )

    def cancel(self, run_id: str) -> bool:
        self._cancel.setdefault(run_id, threading.Event()).set()
        return True

    def unavailable_reason(self, model: str | None = None) -> str | None:
        return self.quota_reason

    def clear_quota(self) -> None:
        self.quota_reason = None

    def health(self, deep: bool = False) -> AgentHealth:
        if self.quota_reason:
            return AgentHealth(self.name, False, "quota_exceeded", self.quota_reason, self.model, {"mock": True})
        return AgentHealth(self.name, True, "ok", "MOCK — não é integração real.", self.model, {"mock": True})


class ScriptedBackend(MockProvider, AgentBackend):
    """Adaptador de backend com script de respostas programadas."""

    def run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        return self.execute(prompt, model=self.model, context_key=context_key, run_id=run_id, options=options)


DEFAULT_TASK = {"id": "T1", "assigned_to": "deepseek", "category": "boilerplate", "assignment_reason": "código mecânico (mock)",
                "title": "Implementar módulo de exemplo", "objective": "Criar módulo Luau de exemplo",
                "instructions": "Criar src/shared/Example.lua", "files": ["src/shared/Example.lua"],
                "acceptance_criteria": ["Módulo retorna tabela"], "constraints": ["Luau estrito"], "depends_on": []}

# Evidência que casa com o conteúdo escrito pelos implementadores simulados dos testes ("return { version = N }").
DEFAULT_FINDING = {"file": "src/shared/Example.lua", "line": 1, "snippet": "return {", "issue": "ajuste simulado",
                   "severity": "major", "checklist": "acceptance_criteria"}


def plan_json(tasks: list[dict] | None = None, decisions: list[dict] | None = None) -> str:
    return "Plano:\n```json\n" + json.dumps({
        "summary": "Plano simulado",
        "tasks": tasks or [DEFAULT_TASK],
        "decisions": decisions if decisions is not None else
        [{"title": "Módulos em src/shared", "decision": "Código compartilhado fica em src/shared", "rationale": "mock"}],
        "risks": [],
    }) + "\n```"


def review_json(verdict: str, finding: dict | None = None) -> str:
    needs_evidence = verdict in {"changes_requested", "rejected"}
    return json.dumps({"verdict": verdict, "summary": f"revisão simulada: {verdict}",
                       "findings": [finding or DEFAULT_FINDING] if needs_evidence else [],
                       "requested_changes": ["ajuste simulado"] if verdict == "changes_requested" else [],
                       "questions": ["por quê?"] if verdict == "needs_clarification" else [],
                       "rejection_reason": "abordagem inadequada (mock)" if verdict == "rejected" else "",
                       "plan_valid": True})


def symmetric_responder(name: str, review_script: list[str] | None = None, files: list[str] | None = None,
                        tasks: list[dict] | None = None, assignment: str | dict = "accept") -> Responder:
    """Agente simulado com as cinco skills. `assignment`: 'accept' ou {task_id: agente_contraproposto}."""
    script = list(review_script or ["approved"])

    def respond(skill: str, prompt: str, calls: dict) -> str:
        if skill == "plan_feature":
            return plan_json(tasks)
        if skill == "respond_assignment":
            ids = [t["id"] for t in (tasks or [DEFAULT_TASK])]
            if assignment == "accept":
                return json.dumps({"responses": [{"task_id": i, "decision": "accept", "reason": "ok"} for i in ids]})
            return json.dumps({"responses": [
                {"task_id": i, "decision": "counter", "proposed_agent": assignment[i], "reason": "contraproposta simulada"}
                if i in assignment else {"task_id": i, "decision": "accept"} for i in ids]})
        if skill == "review_implementation":
            return review_json(script[min(calls["count"] - 1, len(script) - 1)])
        if skill == "final_review":
            return json.dumps({"verdict": "approved", "summary": "revisão final simulada", "findings": []})
        return json.dumps({"status": "done", "summary": f"{name} implementou (mock) #{calls['count']}",
                           "files_modified": files if files is not None else ["src/shared/Example.lua"],
                           "technical_decisions": ["Usar ModuleScript (mock)"], "issues": [],
                           "answers": ["resposta simulada"] if "Perguntas" in prompt else [], "tests": [], "result": "ok"})

    return respond


# Compatibilidade com os testes/demonstração da v0.3.
def default_gemini_responder(review_script: list[str] | None = None) -> Responder:
    return symmetric_responder("gemini", review_script, files=[])


def default_deepseek_responder(files: list[str] | None = None) -> Responder:
    return symmetric_responder("deepseek", files=files)


def build_mock_backends(review_script: list[str] | None = None, delay: float = 0.0,
                        deepseek_review_script: list[str] | None = None) -> dict[str, ScriptedBackend]:
    return {
        "gemini": ScriptedBackend("gemini", "gemini-4-argon-high (MOCK)", symmetric_responder("gemini", review_script, files=[]), delay),
        "deepseek": ScriptedBackend("deepseek", "opencode/deepseek-v4.1-flash-free (MOCK)",
                                    symmetric_responder("deepseek", deepseek_review_script), delay),
    }
