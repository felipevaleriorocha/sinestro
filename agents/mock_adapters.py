"""Backends SIMULADOS para testes e demonstração (ORCH_MOCK_AGENTS=true).

Nada aqui prova integração real com os modelos. As respostas seguem os contratos
JSON dos prompts para exercitar o fluxo A2A completo (planejar, delegar, revisar, corrigir).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from agents.base import AgentBackend, AgentCancelledError, AgentHealth, AgentResult, QuotaExceededError


class ScriptedBackend(AgentBackend):
    kind = "mock"

    def __init__(self, name: str, model: str, responder: Callable[[str, str, dict], str] | None = None, delay: float = 0.0):
        self.name = name
        self._model = model
        self.responder = responder or (lambda skill, prompt, calls: json.dumps({"summary": "ok"}))
        self.delay = delay
        self.calls: list[dict] = []
        self.quota_reason: str | None = None
        self.raise_on_run: Exception | None = None
        self._cancel: dict[str, threading.Event] = {}

    @property
    def model(self) -> str:
        return self._model

    def run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        options = options or {}
        skill = options.get("skill", "")
        self.calls.append({"skill": skill, "context_key": context_key, "run_id": run_id, "prompt": prompt})
        if self.quota_reason:
            raise QuotaExceededError(self.quota_reason)
        if self.raise_on_run:
            raise self.raise_on_run
        ev = self._cancel.setdefault(run_id, threading.Event())
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if ev.wait(0.02):
                raise AgentCancelledError("cancelado (mock)")
        text = self.responder(skill, prompt, {"count": len([c for c in self.calls if c["skill"] == skill])})
        return AgentResult(text=text, model=self.model, session_id=f"mock-{context_key[:8]}", usage={"input_tokens": 1, "output_tokens": 1},
                           duration_seconds=round(self.delay, 3))

    def cancel(self, run_id: str) -> bool:
        self._cancel.setdefault(run_id, threading.Event()).set()
        return True

    def unavailable_reason(self) -> str | None:
        return self.quota_reason

    def health(self, deep: bool = False) -> AgentHealth:
        if self.quota_reason:
            return AgentHealth(self.name, False, "quota_exceeded", self.quota_reason, self.model, {"mock": True})
        return AgentHealth(self.name, True, "ok", "MOCK — não é integração real.", self.model, {"mock": True})


def default_gemini_responder(review_script: list[str] | None = None) -> Callable[[str, str, dict], str]:
    """Tech Lead simulado. review_script define os vereditos sucessivos (ex.: ['changes_requested', 'approved'])."""
    script = list(review_script or ["approved"])

    def respond(skill: str, prompt: str, calls: dict) -> str:
        if skill == "plan_feature":
            return "Plano:\n```json\n" + json.dumps({
                "summary": "Plano simulado",
                "tasks": [{"id": "T1", "agent": "deepseek", "title": "Implementar módulo de exemplo",
                           "objective": "Criar módulo Luau de exemplo", "instructions": "Criar src/shared/Example.lua",
                           "files": ["src/shared/Example.lua"], "acceptance_criteria": ["Módulo retorna tabela"],
                           "constraints": ["Luau estrito"], "depends_on": []}],
                "decisions": [{"title": "Módulos em src/shared", "decision": "Código compartilhado fica em src/shared", "rationale": "mock"}],
                "risks": [],
            }) + "\n```"
        if skill in {"review_implementation", "final_review"}:
            idx = min(calls["count"] - 1, len(script) - 1) if skill == "review_implementation" else len(script) - 1
            verdict = script[idx] if skill == "review_implementation" else "approved"
            return json.dumps({"verdict": verdict, "summary": f"revisão simulada: {verdict}",
                               "findings": ["mock"], "requested_changes": ["ajuste simulado"] if verdict == "changes_requested" else [],
                               "questions": ["por quê?"] if verdict == "needs_clarification" else [],
                               "rejection_reason": "abordagem inadequada (mock)" if verdict == "rejected" else ""})
        return json.dumps({"status": "done", "summary": "Gemini executou (mock)", "files_modified": [], "result": "ok"})

    return respond


def default_deepseek_responder(files: list[str] | None = None) -> Callable[[str, str, dict], str]:
    def respond(skill: str, prompt: str, calls: dict) -> str:
        return json.dumps({"status": "done", "summary": f"Implementação simulada #{calls['count']}",
                           "files_modified": files if files is not None else ["src/shared/Example.lua"],
                           "technical_decisions": ["Usar ModuleScript (mock)"], "issues": [],
                           "answers": ["resposta simulada"] if "Perguntas" in prompt else [], "tests": [], "result": "ok"})
    return respond


def build_mock_backends(review_script: list[str] | None = None, delay: float = 0.0) -> dict[str, ScriptedBackend]:
    return {
        "gemini": ScriptedBackend("gemini", "gemini-4-argon-high (MOCK)", default_gemini_responder(review_script), delay),
        "deepseek": ScriptedBackend("deepseek", "opencode/deepseek-v4.1-flash-free (MOCK)", default_deepseek_responder(), delay),
    }
