"""Servidores A2A (SDK oficial a2a-sdk, protocolo 1.0) para Gemini e DeepSeek.

Cada agente é exposto com identidade própria:
  GET  /a2a/<agente>/.well-known/agent-card.json   Agent Card
  POST /a2a/<agente>/                              JSON-RPC A2A (SendMessage, GetTask, CancelTask, ...)
As tarefas A2A são persistidas pelo DatabaseTaskStore oficial na tabela padrão `tasks` do SDK em orchestrator_a2a.db.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from a2a.helpers import new_data_part, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill, Message, TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from pydantic import ValidationError

from agents.base import READ_ONLY_SKILLS, AgentBackend, AgentCancelledError, AgentError, AgentResponseError, AgentResult, parse_json_object
from communication.messaging import parse_request
from core.domain import DomainProfile
from core.logging_setup import redact
from database.repository import Repository
from orchestrator.agent_registry import AgentDefinition
from orchestrator.schemas import AssignmentResponse, ImplementationReport, Plan, ReviewResult

log = logging.getLogger("a2a.server")
VERSION = "0.5.0"


def agent_base_path(name: str) -> str:
    return f"/a2a/{name}"


def build_agent_card(defn: AgentDefinition, public_url: str, model: str) -> AgentCard:
    url = f"{public_url.rstrip('/')}{agent_base_path(defn.name)}/"
    return AgentCard(
        name=defn.display_name,
        description=f"{defn.role}. {defn.description} Modelo: {model}.",
        version=VERSION,
        provider=AgentProvider(organization=defn.provider, url=public_url),
        supported_interfaces=[AgentInterface(url=url, protocol_binding=TransportProtocol.JSONRPC.value, protocol_version=PROTOCOL_VERSION_1_0)],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(id=s.id, name=s.name, description=s.description, tags=list(s.tags) or [defn.name], examples=list(s.examples))
            for s in defn.skills
        ],
    )


def _status_message(text: str, task_id: str, context_id: str, meta: dict | None = None) -> Message:
    msg = new_text_message(text, context_id=context_id, task_id=task_id)
    if meta:
        msg.metadata.update(meta)
    return msg


class RoleAgentExecutor(AgentExecutor):
    """Executor A2A: recebe a tarefa estruturada, monta o prompt do papel, chama o backend e publica artefatos."""

    def __init__(self, defn: AgentDefinition, backend: AgentBackend, repo: Repository,
                 domain: DomainProfile | None = None):
        self.defn = defn
        self.backend = backend
        self.repo = repo
        self.domain = domain

    # ---------------------------------------------------------------- prompts por skill
    def _prompt(self, skill: str, p: dict) -> str:
        me = self.defn.name
        other = "deepseek" if me == "gemini" else "gemini"
        if self.domain is None:
            raise AgentResponseError("Nenhum DomainProfile configurado no executor.")
        pr = self.domain.get_prompts()
        if skill == "plan_feature":
            return pr.plan_prompt(me, p["goal"], p.get("context_text", ""), int(p.get("max_subtasks", 5)), p.get("mcp_status"),
                                  p.get("validation_errors"), p.get("previous_plan"))
        if skill == "respond_assignment":
            return pr.respond_assignment_prompt(me, p.get("planner", other), p.get("proposals", []), p.get("goal", ""),
                                                p.get("context_text", ""))
        if skill == "implement_task":
            return pr.implement_prompt(me, p["task"], p["goal"], p.get("context_text", ""), p.get("feedback"),
                                       int(p.get("cycle", 0)), p.get("previous_results", []), p.get("mcp_status"),
                                       p.get("reviewer") or other)
        if skill == "review_implementation":
            return pr.review_prompt(me, p.get("implementer") or other, p["task"], p.get("report", {}), p["goal"],
                                    p.get("context_text", ""), int(p.get("cycle", 1)), int(p.get("max_cycles", 1)),
                                    p.get("git_changes", []), p.get("checks"), p.get("mcp_status"), p.get("validate_plan"),
                                    p.get("validation_errors"), p.get("previous_review"))
        if skill == "final_review":
            return pr.final_review_prompt(me, p["goal"], p.get("subtasks", []), p.get("context_text", ""),
                                          p.get("self_authored"), p.get("watched_files"), p.get("validation_errors"),
                                          p.get("previous_review"))
        raise AgentResponseError(f"Skill não suportada: {skill}")

    @staticmethod
    def _structure(skill: str, text: str) -> tuple[dict, str]:
        """Valida a resposta contra o contrato da skill. Retorna (resultado, resumo)."""
        if skill == "plan_feature":
            try:
                plan = Plan.model_validate(parse_json_object(text))
            except (ValidationError, AgentResponseError) as exc:
                raise AgentResponseError(f"Plano inválido: {redact(exc)[:500]}") from exc
            if not plan.tasks:
                raise AgentResponseError("O plano não contém tarefas.")
            return plan.model_dump(), plan.summary or f"{len(plan.tasks)} subtarefas"
        if skill == "implement_task":
            try:
                rep = ImplementationReport.model_validate(parse_json_object(text))
            except (ValidationError, AgentResponseError):
                rep = ImplementationReport(
                    status="partial",
                    summary="Implementação não retornou formato JSON estruturado válido.",
                    result="Resposta não estruturada.",
                    raw_text=text[:4000],
                    structured=False
                )
            return rep.model_dump(), rep.summary[:300]
        if skill == "respond_assignment":
            try:
                resp = AssignmentResponse.model_validate(parse_json_object(text))
            except (ValidationError, AgentResponseError):
                resp = AssignmentResponse(responses=[], structured=False)
            counters = sum(1 for r in resp.responses if r.decision == "counter")
            return resp.model_dump(), f"{len(resp.responses)} respostas, {counters} contestações"
        if skill in {"review_implementation", "final_review"}:
            try:
                rev = ReviewResult.model_validate(parse_json_object(text))
            except (ValidationError, AgentResponseError) as exc:
                log.warning("Revisão fora do formato JSON: %s", redact(exc)[:500])
                rev = ReviewResult(
                    verdict="changes_requested",
                    summary="Revisão não retornou formato JSON estruturado válido.",
                    raw_text=text[:2000],
                    structured=False
                )
            return rev.model_dump(), f"{rev.verdict}: {rev.summary[:250]}"
        raise AgentResponseError(f"Skill não suportada: {skill}")

    # ---------------------------------------------------------------- A2A
    async def execute(self, context: RequestContext, event_queue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        skill, payload = parse_request(context.message)
        meta = context.metadata or {}
        wf, st = meta.get("workflow_id"), meta.get("subtask_id")
        run_info = {"agent": self.defn.name, "model": self.backend.model, "a2a_task_id": task.id, "workflow_id": wf,
                    "subtask_id": st, "skill": skill}

        if not skill or skill not in self.defn.skill_ids():
            await updater.update_status(TaskState.TASK_STATE_REJECTED, _status_message(
                f"Skill '{skill}' não suportada por {self.defn.display_name}.", task.id, task.context_id, {"error_code": "unsupported_skill"}))
            self.repo.add_run(**run_info, status="rejected", error=f"skill {skill} não suportada")
            return
        reason = self.backend.unavailable_reason()
        if reason:
            await updater.update_status(TaskState.TASK_STATE_REJECTED, _status_message(
                reason, task.id, task.context_id, {"error_code": "quota_exceeded", "retryable": True}))
            self.repo.add_run(**run_info, status="rejected", error=reason)
            return

        await updater.start_work(_status_message(f"{self.defn.name} iniciou {skill}.", task.id, task.context_id))
        started = time.monotonic()
        try:
            prompt = self._prompt(skill, payload)
            result: AgentResult = await asyncio.to_thread(
                self.backend.run, prompt, context_key=task.context_id, run_id=task.id,
                options={"skill": skill, "read_only": skill in READ_ONLY_SKILLS,
                         "title": f"{skill} {wf or ''} {st or ''}".strip(), **(payload.get("agent_options") or {})},
            )
            structured, summary = self._structure(skill, result.text)
        except asyncio.CancelledError:
            self.backend.cancel(task.id)
            self.repo.add_run(**run_info, status="canceled", duration_seconds=round(time.monotonic() - started, 3))
            raise
        except AgentCancelledError as exc:
            self.repo.add_run(**run_info, status="canceled", error=str(exc))
            await updater.cancel(_status_message(str(exc), task.id, task.context_id, {"error_code": exc.code}))
            return
        except AgentError as exc:
            msg = redact(str(exc))
            self.repo.add_run(**run_info, status="failed", error=f"{exc.code}: {msg}",
                              duration_seconds=round(time.monotonic() - started, 3))
            await updater.failed(_status_message(msg, task.id, task.context_id, {"error_code": exc.code, "retryable": exc.retryable}))
            return
        except Exception as exc:  # noqa: BLE001
            msg = redact(f"{type(exc).__name__}: {exc}")
            log.exception("Erro inesperado no executor %s", self.defn.name)
            self.repo.add_run(**run_info, status="failed", error=msg)
            await updater.failed(_status_message(msg, task.id, task.context_id, {"error_code": "internal_error"}))
            return

        usage_meta = {"model": result.model, "duration_seconds": result.duration_seconds, "session_id": result.session_id,
                      "num_turns": result.num_turns}
        await updater.add_artifact(
            [new_data_part({"skill": skill, "result": structured, "run": {k: v for k, v in usage_meta.items() if v is not None},
                            "usage": result.usage}),
             new_text_part(result.text)],
            name=f"{skill}_result",
        )
        self.repo.add_run(**run_info, status="completed", session_id=result.session_id, duration_seconds=result.duration_seconds,
                          usage=result.usage)
        await updater.complete(_status_message(summary or "concluído", task.id, task.context_id))

    async def cancel(self, context: RequestContext, event_queue) -> None:
        if context.task_id:
            self.backend.cancel(context.task_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel(_status_message("Cancelado a pedido do orquestrador.", context.task_id, context.context_id,
                                             {"error_code": "cancelled"}))


def mount_a2a_agents(app, *, definitions: dict[str, AgentDefinition], backends: dict[str, AgentBackend], repo: Repository,
                     task_store, public_url: str, domain: DomainProfile | None = None) -> dict[str, AgentCard]:
    """Monta Agent Card + JSON-RPC de cada agente no FastAPI usando os componentes oficiais do SDK."""
    cards: dict[str, AgentCard] = {}
    for name, backend in backends.items():
        defn = definitions[name]
        card = build_agent_card(defn, public_url, backend.model)
        handler = DefaultRequestHandler(agent_executor=RoleAgentExecutor(defn, backend, repo, domain=domain),
                                        task_store=task_store, agent_card=card)
        base = agent_base_path(name)
        add_a2a_routes_to_fastapi(
            app,
            agent_card_routes=create_agent_card_routes(card, card_url=f"{base}/.well-known/agent-card.json"),
            jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=f"{base}/"),
        )
        cards[name] = card
    return cards


def card_to_dict(card: AgentCard) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict
    return MessageToDict(card)

