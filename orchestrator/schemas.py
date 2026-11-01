"""Contratos estruturados trocados entre os agentes (payloads dos DataParts A2A)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgentName = Literal["gemini", "deepseek"]


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) if not isinstance(x, dict) else (x.get("path") or x.get("text") or str(x)) for x in v if x is not None]
    return [str(v)]


class DecisionProposal(BaseModel):
    title: str
    decision: str
    rationale: str = ""
    tags: list[str] = Field(default_factory=list)


class PlannedTask(BaseModel):
    id: str = ""
    agent: AgentName = "deepseek"
    title: str
    objective: str = ""
    instructions: str = ""
    context: str = ""
    files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("agent", mode="before")
    @classmethod
    def _agent(cls, v: Any) -> str:
        v = str(v or "deepseek").strip().lower()
        return v if v in {"gemini", "deepseek"} else "deepseek"

    @field_validator("files", "acceptance_criteria", "constraints", "depends_on", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)


class Plan(BaseModel):
    summary: str = ""
    tasks: list[PlannedTask]
    decisions: list[DecisionProposal] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @field_validator("risks", mode="before")
    @classmethod
    def _risks(cls, v: Any) -> list[str]:
        return _str_list(v)


class ImplementationReport(BaseModel):
    status: Literal["done", "partial", "blocked", "failed"] = "done"
    summary: str = ""
    files_modified: list[str] = Field(default_factory=list)
    technical_decisions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    result: str = ""
    structured: bool = True

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str:
        v = str(v or "done").lower()
        return v if v in {"done", "partial", "blocked", "failed"} else "done"

    @field_validator("files_modified", "technical_decisions", "issues", "answers", "tests", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)


Verdict = Literal["approved", "changes_requested", "needs_clarification", "rejected"]


class ReviewResult(BaseModel):
    verdict: Verdict
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    requested_changes: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    rejection_reason: str = ""
    decisions: list[DecisionProposal] = Field(default_factory=list)
    structured: bool = True

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v: Any) -> str:
        v = str(v or "").strip().lower().replace(" ", "_")
        aliases = {"approve": "approved", "ok": "approved", "changes": "changes_requested", "request_changes": "changes_requested",
                   "clarification": "needs_clarification", "questions": "needs_clarification", "reject": "rejected"}
        v = aliases.get(v, v)
        return v if v in {"approved", "changes_requested", "needs_clarification", "rejected"} else "changes_requested"

    @field_validator("findings", "requested_changes", "questions", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)
