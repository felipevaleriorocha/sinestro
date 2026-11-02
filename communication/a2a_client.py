"""Cliente A2A (SDK oficial): descoberta por Agent Card, envio, acompanhamento e cancelamento de tarefas.

Todo envio/recebimento é registrado em `agent_messages` (histórico de comunicação).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.client.errors import A2AClientError
from a2a.types import AgentCard, CancelTaskRequest, GetTaskRequest, Task

from communication.messaging import (
    INTERRUPTED_STATES,
    TERMINAL_STATES,
    DelegationResult,
    build_request,
    result_from_task,
    state_name,
    summarize_payload,
)
from core.logging_setup import redact
from database.repository import Repository

log = logging.getLogger("a2a.client")


class A2ADelegationError(RuntimeError):
    def __init__(self, message: str, code: str = "a2a_error"):
        super().__init__(message)
        self.code = code


class A2AAgentClient:
    def __init__(self, repo: Repository, agent_urls: dict[str, str], *, sender: str = "orchestrator",
                 http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
                 poll_interval: float = 2.0, task_timeout: float = 3600.0):
        self.repo = repo
        self.agent_urls = dict(agent_urls)  # nome -> URL base (onde está /.well-known/agent-card.json)
        self.sender = sender
        self._http_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)))
        self._http: httpx.AsyncClient | None = None
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.cards: dict[str, AgentCard] = {}
        self._clients: dict[str, Client] = {}

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    async def close(self) -> None:
        for c in self._clients.values():
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---------------------------------------------------------------- descoberta
    async def discover(self, name: str, refresh: bool = False) -> AgentCard:
        if name in self.cards and not refresh:
            return self.cards[name]
        base = self.agent_urls.get(name)
        if not base:
            raise A2ADelegationError(f"Agente '{name}' não registrado para descoberta.", "unknown_agent")
        try:
            card = await A2ACardResolver(self.http(), base).get_agent_card()
        except Exception as exc:  # noqa: BLE001
            raise A2ADelegationError(f"Falha ao obter Agent Card de {name} em {base}: {redact(exc)}", "discovery_failed") from exc
        self.cards[name] = card
        self._clients.pop(name, None)
        return card

    async def discover_all(self) -> dict[str, dict]:
        out = {}
        for name in self.agent_urls:
            try:
                card = await self.discover(name, refresh=True)
                out[name] = {"reachable": True, "name": card.name, "skills": [s.id for s in card.skills],
                             "url": card.supported_interfaces[0].url if card.supported_interfaces else None}
            except A2ADelegationError as exc:
                out[name] = {"reachable": False, "error": str(exc)}
        return out

    async def _client(self, name: str) -> Client:
        if name not in self._clients:
            card = await self.discover(name)
            # Sem streaming: envio retorna a Task imediatamente (return_immediately) e acompanhamos via GetTask.
            factory = ClientFactory(ClientConfig(streaming=False, polling=True, httpx_client=self.http()))
            self._clients[name] = factory.create(card)
        return self._clients[name]

    # ---------------------------------------------------------------- tarefas
    async def send(self, agent: str, skill: str, payload: dict, *, summary: str, context_id: str | None = None,
                   reference_task_ids: list[str] | None = None, workflow_id: str | None = None,
                   subtask_id: str | None = None, sender: str | None = None) -> tuple[str, str]:
        sender = sender or self.sender
        card = await self.discover(agent)
        if skill not in {s.id for s in card.skills}:
            raise A2ADelegationError(f"{agent} não anuncia a skill '{skill}' no Agent Card.", "unsupported_skill")
        req = build_request(skill, payload, sender=sender, summary=summary, context_id=context_id,
                            reference_task_ids=reference_task_ids, metadata={"workflow_id": workflow_id, "subtask_id": subtask_id})
        client = await self._client(agent)
        task: Task | None = None
        try:
            async for event in client.send_message(req):
                if event.HasField("task"):
                    task = event.task
                    break
                if event.HasField("message"):
                    raise A2ADelegationError(f"{agent} respondeu com mensagem direta em vez de tarefa.", "unexpected_message")
        except A2AClientError as exc:
            self._log("error", agent, skill, workflow_id, subtask_id, req.message.message_id, None, context_id, None, str(exc), None, sender)
            raise A2ADelegationError(f"Falha A2A ao enviar para {agent}: {redact(exc)}", "send_failed") from exc
        if task is None:
            raise A2ADelegationError(f"{agent} não retornou tarefa.", "no_task")
        self._log("outbound", agent, skill, workflow_id, subtask_id, req.message.message_id, task.id, task.context_id,
                  state_name(task.status.state), summary, summarize_payload(skill, payload), sender)
        return task.id, task.context_id

    async def wait(self, agent: str, task_id: str, *, skill: str, workflow_id: str | None = None, subtask_id: str | None = None,
                   should_cancel: Callable[[], bool] | None = None, on_poll: Callable[[], Awaitable[None]] | None = None,
                   requester: str | None = None) -> DelegationResult:
        client = await self._client(agent)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.task_timeout
        cancel_sent = False
        while True:
            try:
                task = await client.get_task(GetTaskRequest(id=task_id))
            except A2AClientError as exc:
                raise A2ADelegationError(f"Falha A2A ao consultar tarefa {task_id}: {redact(exc)}", "get_task_failed") from exc
            if task.status.state in TERMINAL_STATES or task.status.state in INTERRUPTED_STATES:
                result = result_from_task(agent, skill, task)
                self._log("inbound", agent, skill, workflow_id, subtask_id, None, task.id, task.context_id, result.state,
                          result.status_message or result.text[:500],
                          {"data": result.data, "error_code": result.error_code, "artifacts": result.artifacts}, requester)
                return result
            if should_cancel and should_cancel() and not cancel_sent:
                await self.cancel(agent, task_id, workflow_id=workflow_id, subtask_id=subtask_id)
                cancel_sent = True
            if loop.time() > deadline:
                await self.cancel(agent, task_id, workflow_id=workflow_id, subtask_id=subtask_id)
                raise A2ADelegationError(f"Tarefa {task_id} de {agent} excedeu {self.task_timeout}s.", "timeout")
            if on_poll:
                await on_poll()
            await asyncio.sleep(self.poll_interval)

    async def cancel(self, agent: str, task_id: str, *, workflow_id: str | None = None, subtask_id: str | None = None) -> str | None:
        client = await self._client(agent)
        try:
            task = await client.cancel_task(CancelTaskRequest(id=task_id))
            state = state_name(task.status.state)
        except A2AClientError as exc:
            state = None
            log.warning("Cancelamento A2A falhou para %s/%s: %s", agent, task_id, redact(exc))
        self._log("cancel", agent, None, workflow_id, subtask_id, None, task_id, None, state, "pedido de cancelamento", None)
        return state

    async def delegate(self, agent: str, skill: str, payload: dict, *, summary: str, **kw) -> DelegationResult:
        task_id, _ = await self.send(agent, skill, payload, summary=summary, context_id=kw.get("context_id"),
                                     reference_task_ids=kw.get("reference_task_ids"), workflow_id=kw.get("workflow_id"),
                                     subtask_id=kw.get("subtask_id"), sender=kw.get("sender"))
        if kw.get("on_task"):
            await kw["on_task"](task_id)
        return await self.wait(agent, task_id, skill=skill, workflow_id=kw.get("workflow_id"), subtask_id=kw.get("subtask_id"),
                               should_cancel=kw.get("should_cancel"), requester=kw.get("sender"))

    def _log(self, direction, agent, skill, workflow_id, subtask_id, message_id, task_id, context_id, state, summary, payload,
             party: str | None = None) -> None:
        outbound = direction in {"outbound", "cancel", "error"}
        party = party or self.sender
        self.repo.add_message(
            workflow_id=workflow_id, subtask_id=subtask_id, direction=direction,
            sender=party if outbound else agent, recipient=agent if outbound else party, skill=skill,
            a2a_message_id=message_id, a2a_task_id=task_id, a2a_context_id=context_id, a2a_state=state,
            summary=summary, payload=payload,
        )
