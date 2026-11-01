"""Contratos estruturados trocados entre os agentes (payloads dos DataParts A2A)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AgentName = Literal["gemini", "deepseek"]


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) if not isinstance(x, dict) else (x.get("path") or x.get("text") or str(x)) for x in v if x is not None]
    return [str(v)]


class _Lenient(BaseModel):
    """Modelos costumam mandar null em campos opcionais: null = campo ausente (vale o padrão)."""

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class DecisionProposal(_Lenient):
    title: str
    decision: str
    rationale: str = ""
    tags: list[str] = Field(default_factory=list)


CATEGORIES = (
    "architecture", "security", "server", "persistence", "events", "ui",
    "boilerplate", "mechanical", "config", "asset", "assets", "docs", "other",
)


class PlannedTask(_Lenient):
    id: str = ""
    agent: AgentName = "deepseek"          # compatível com planos da v0.3; espelha assigned_to
    assigned_to: AgentName = "deepseek"    # PROPOSTA do planejador (turno 1); a decisão final é AssignmentDecision
    category: str = "other"
    skip_review: bool = False
    assignment_reason: str = ""
    title: str
    objective: str = ""
    instructions: str = ""
    context: str = ""
    files: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    contracts: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _proposal(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data.pop("reviewer", None)  # revisor nunca vem do modelo: é sempre "o outro agente"
            proposed = data.get("assigned_to") or data.get("agent") or "deepseek"
            data["assigned_to"] = data["agent"] = proposed
        return data

    @field_validator("agent", "assigned_to", mode="before")
    @classmethod
    def _agent(cls, v: Any) -> str:
        v = str(v or "deepseek").strip().lower()
        return v if v in {"gemini", "deepseek"} else "deepseek"

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> str:
        v = str(v or "other").strip().lower()
        return v if v in CATEGORIES else "other"

    @field_validator("files", "reads", "contracts", "resources", "uncertain", "acceptance_criteria", "constraints", "depends_on", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)


class Plan(_Lenient):
    summary: str = ""
    planned_by: str = ""
    tasks: list[PlannedTask]
    decisions: list[DecisionProposal] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @field_validator("risks", mode="before")
    @classmethod
    def _risks(cls, v: Any) -> list[str]:
        return _str_list(v)


class ImplementationReport(_Lenient):
    status: Literal["done", "partial", "blocked", "failed"] = "done"
    summary: str = ""
    files_modified: list[str] = Field(default_factory=list)
    technical_decisions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    answers: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    result: str = ""
    disputes: list[str] = Field(default_factory=list)  # contestações a achados do revisor (com motivo)
    structured: bool = True

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str:
        v = str(v or "done").lower()
        return v if v in {"done", "partial", "blocked", "failed"} else "done"

    @field_validator("files_modified", "technical_decisions", "issues", "answers", "tests", "disputes", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)


Verdict = Literal["approved", "changes_requested", "needs_clarification", "rejected"]
CHECKLIST = ("acceptance_criteria", "api_misuse", "edge_case", "client_server_security", "other")


class ReviewFinding(_Lenient):
    """Achado de revisão com evidência verificável (arquivo + linha + trecho literal)."""
    file: str = ""
    line: int = 0
    snippet: str = ""
    issue: str = ""
    severity: Literal["blocker", "major", "minor"] = "major"
    checklist: str = "other"

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"issue": data}  # sem evidência -> será descartado na validação
        return data

    @field_validator("line", mode="before")
    @classmethod
    def _line(cls, v: Any) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v: Any) -> str:
        v = str(v or "major").lower()
        return v if v in {"blocker", "major", "minor"} else "major"

    @field_validator("checklist", mode="before")
    @classmethod
    def _chk(cls, v: Any) -> str:
        v = str(v or "other").lower()
        return v if v in CHECKLIST else "other"


class ReviewResult(_Lenient):
    verdict: Verdict
    summary: str = ""
    findings: list[ReviewFinding] = Field(default_factory=list)
    requested_changes: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    rejection_reason: str = ""
    decisions: list[DecisionProposal] = Field(default_factory=list)
    plan_valid: bool | None = None          # só quando o plano foi feito por fallback
    plan_concerns: list[str] = Field(default_factory=list)
    structured: bool = True

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v: Any) -> str:
        v = str(v or "").strip().lower().replace(" ", "_")
        aliases = {"approve": "approved", "ok": "approved", "changes": "changes_requested", "request_changes": "changes_requested",
                   "clarification": "needs_clarification", "questions": "needs_clarification", "reject": "rejected"}
        v = aliases.get(v, v)
        return v if v in {"approved", "changes_requested", "needs_clarification", "rejected"} else "changes_requested"

    @field_validator("findings", mode="before")
    @classmethod
    def _findings(cls, v: Any) -> list:
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    @field_validator("requested_changes", "questions", "plan_concerns", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _str_list(v)


# ---------------------------------------------------------------- atribuição (proposta -> aceite/contraproposta -> desempate)
class AssignmentProposal(BaseModel):
    task_id: str
    assigned_to: AgentName
    category: str = "other"
    reason: str = ""
    proposed_by: AgentName


class AssignmentResponseItem(_Lenient):
    task_id: str
    decision: Literal["accept", "counter"] = "accept"
    proposed_agent: AgentName | None = None
    reason: str = ""

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, v: Any) -> str:
        v = str(v or "accept").strip().lower()
        return "counter" if v in {"counter", "contest", "contestar", "reject", "contraproposta"} else "accept"

    @field_validator("proposed_agent", mode="before")
    @classmethod
    def _agent(cls, v: Any) -> Any:
        if v in (None, ""):
            return None
        v = str(v).strip().lower()
        return v if v in {"gemini", "deepseek"} else None


class AssignmentResponse(_Lenient):
    responses: list[AssignmentResponseItem] = Field(default_factory=list)
    structured: bool = True


DecidedBy = Literal["consensus", "rules", "quota_fallback", "no_negotiation", "human"]


class AssignmentDecision(BaseModel):
    task_id: str
    assigned_to: AgentName
    reviewer: AgentName
    proposed_by: AgentName | None = None
    proposed_to: AgentName | None = None
    proposal_reason: str = ""
    response: Literal["accept", "counter", "none"] = "none"
    responder: AgentName | None = None
    counter_reason: str = ""
    decided_by: DecidedBy
    rule: str = ""
    reason: str = ""

    @model_validator(mode="after")
    def _no_self_review(self) -> "AssignmentDecision":
        if self.reviewer == self.assigned_to:
            raise ValueError("reviewer não pode ser igual a assigned_to (quem escreve nunca revisa o próprio trabalho).")
        return self

