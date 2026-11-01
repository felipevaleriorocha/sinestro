"""Rotas REST do orquestrador e do painel."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from communication.a2a_server import agent_base_path, card_to_dict
from orchestrator.task_manager import WF

router = APIRouter()


class TaskRequest(BaseModel):
    goal: str = Field(min_length=8, max_length=8000)
    mode: Literal["full", "plan_only"] = "full"


class DecisionRequest(BaseModel):
    title: str = Field(min_length=3, max_length=300)
    decision: str = Field(min_length=3, max_length=4000)
    rationale: str = ""
    author: str = "user"
    tags: list[str] = Field(default_factory=list)
    status: Literal["proposed", "accepted"] = "accepted"
    supersedes: int | None = None


def C(request: Request):
    return request.app.state.container


@router.get("/health", tags=["Sistema"])
async def health(request: Request) -> dict[str, Any]:
    c = C(request)
    return {"ok": True, "version": "0.3.0", "mock_mode": c.mock_mode, **c.settings.public_summary(),
            "git_repo": await asyncio.to_thread(c.git.is_repo)}


@router.post("/tasks", status_code=202, tags=["Tarefas"])
async def create_task(body: TaskRequest, request: Request) -> dict[str, str]:
    c = C(request)
    wf_id = c.tasks.create_workflow(body.goal, body.mode)
    c.coordinator.start(wf_id)
    return {"id": wf_id, "status": WF.QUEUED, "status_url": f"/tasks/{wf_id}"}


@router.get("/tasks", tags=["Tarefas"])
def list_tasks(request: Request, limit: int = Query(50, le=500), status: str | None = None) -> list[dict]:
    return C(request).repo.list_workflows(limit, status)


@router.get("/tasks/{task_id}", tags=["Tarefas"])
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    events = c.repo.list_events(task_id, limit=2000)
    return {
        "id": wf["id"], "goal": wf["goal"], "status": wf["status"], "phase": wf.get("phase"), "mode": wf.get("mode"),
        "created_at": wf["created_at"], "updated_at": wf["updated_at"], "result": wf.get("result"), "error": wf.get("error"),
        "plan": wf.get("plan"), "context": wf.get("context_sources"), "cancel_requested": wf.get("cancel_requested"),
        "running": c.coordinator.is_running(task_id),
        "subtasks": c.tasks.subtasks(task_id),
        "events": [{k: e[k] for k in ("created_at", "agent", "event", "level", "content", "subtask_id")} for e in events],
    }


@router.post("/tasks/{task_id}/cancel", tags=["Tarefas"])
async def cancel_task(task_id: str, request: Request) -> dict:
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    if wf["status"] not in WF.ACTIVE:
        raise HTTPException(409, f"Tarefa não está ativa (status {wf['status']}).")
    await c.coordinator.cancel(task_id)
    return {"id": task_id, "cancel_requested": True}


@router.post("/tasks/{task_id}/resume", status_code=202, tags=["Tarefas"])
async def resume_task(task_id: str, request: Request) -> dict:
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    if wf["status"] not in WF.RESUMABLE or c.coordinator.is_running(task_id):
        raise HTTPException(409, f"Tarefa não pode ser retomada (status {wf['status']}).")
    if wf["status"] == WF.PLANNED:
        c.repo.update_workflow(task_id, status=WF.QUEUED)
        with c.repo.connect() as con:
            con.execute("UPDATE tasks SET mode='full' WHERE id=?", (task_id,))
    c.coordinator.start(task_id, resume=True)
    return {"id": task_id, "status": "resuming"}


@router.get("/tasks/{task_id}/messages", tags=["Comunicação A2A"])
def task_messages(task_id: str, request: Request) -> list[dict]:
    return C(request).repo.list_messages(task_id, limit=1000)


@router.get("/tasks/{task_id}/events", tags=["Tarefas"])
def task_events(task_id: str, request: Request) -> list[dict]:
    return C(request).repo.list_events(task_id, limit=5000)


@router.get("/agents", tags=["Agentes"])
def list_agents(request: Request) -> list[dict]:
    c = C(request)
    out = c.registry.describe()
    for a in out:
        base = f"{c.settings.public_url}{agent_base_path(a['name'])}"
        a["agent_card_url"] = f"{base}/.well-known/agent-card.json"
        a["a2a_endpoint"] = f"{base}/"
        a["mock"] = c.mock_mode
    return out


@router.post("/agents/{name}/check", tags=["Agentes"])
async def check_agent(name: str, request: Request, deep: bool = False) -> dict:
    c = C(request)
    if name not in c.registry.names():
        raise HTTPException(404, "Agente desconhecido.")
    h = await c.registry.check(name, deep=deep)
    return h.to_dict()


@router.get("/agents/{name}/card", tags=["Agentes"])
def agent_card(name: str, request: Request) -> dict:
    cards = request.app.state.cards
    if name not in cards:
        raise HTTPException(404, "Agente desconhecido.")
    return card_to_dict(cards[name])


@router.get("/a2a/discovery", tags=["Comunicação A2A"])
async def discovery(request: Request) -> dict:
    """Descobre os agentes buscando os Agent Cards via HTTP (A2ACardResolver do SDK)."""
    c = C(request)
    result = await c.a2a.discover_all()
    for name, info in result.items():
        if name in c.registry.agents:
            c.registry.agents[name].card_reachable = info["reachable"]
            c.registry.agents[name].card_error = info.get("error")
    return result


@router.get("/messages", tags=["Comunicação A2A"])
def messages(request: Request, limit: int = Query(200, le=2000)) -> list[dict]:
    return C(request).repo.list_messages(None, limit)


@router.get("/runs", tags=["Agentes"])
def runs(request: Request, agent: str | None = None, limit: int = Query(100, le=1000)) -> list[dict]:
    return C(request).repo.list_runs(agent, limit)


@router.get("/errors", tags=["Sistema"])
def errors(request: Request, limit: int = Query(200, le=2000)) -> dict:
    c = C(request)
    failed_runs = [r for r in c.repo.list_runs(None, 500) if r["status"] in {"failed", "rejected"}][:limit]
    return {"events": c.repo.list_events(None, "error", limit) + c.repo.list_events(None, "warning", limit), "runs": failed_runs}


@router.get("/files/changes", tags=["Arquivos"])
def file_changes(request: Request, workflow_id: str | None = None, limit: int = Query(300, le=5000)) -> list[dict]:
    return C(request).repo.list_file_changes(workflow_id, limit)


@router.get("/locks", tags=["Arquivos"])
def locks(request: Request) -> list[dict]:
    return C(request).locks.list_locks()


@router.get("/locks/history", tags=["Arquivos"])
def locks_history(request: Request, limit: int = Query(200, le=5000)) -> list[dict]:
    return C(request).locks.history(limit)


@router.post("/locks/release-expired", tags=["Arquivos"])
def release_expired(request: Request) -> dict:
    return {"released": C(request).locks.release_expired()}


@router.get("/decisions", tags=["Memória"])
def list_decisions(request: Request, status: str | None = None, q: str | None = None, limit: int = Query(100, le=1000)) -> list[dict]:
    return C(request).decisions.list(status, q, limit)


@router.post("/decisions", status_code=201, tags=["Memória"])
def add_decision(body: DecisionRequest, request: Request) -> dict:
    new_id = C(request).decisions.add(body.title, body.decision, body.author, body.rationale, body.status, body.tags,
                                      supersedes=body.supersedes)
    return {"id": new_id}


@router.get("/context", tags=["Memória"])
async def context(request: Request, include_text: bool = False) -> dict:
    c = C(request)
    bundle = await asyncio.to_thread(c.context.build)
    return bundle.to_dict(include_text)


@router.get("/checks/status", tags=["Integrações"])
async def checks_status(request: Request) -> dict:
    """Checagens automáticas configuradas/instaladas (sem executar nada)."""
    return await asyncio.to_thread(C(request).checks.status)


@router.get("/mcp/status", tags=["Integrações"])
async def mcp_status(request: Request) -> dict:
    return await asyncio.to_thread(C(request).mcp.status)


@router.get("/integrations/opencode/contract", tags=["Integrações"])
async def opencode_contract(request: Request, refresh: bool = False) -> dict:
    backend = C(request).backends.get("deepseek")
    contract = getattr(backend, "contract", None)
    if contract is None:
        return {"available": False, "detail": "Backend DeepSeek não é o OpenCodeAdapter (modo mock?)."}
    if refresh:
        try:
            return await asyncio.to_thread(backend.discover_spec)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Falha ao consultar OpenCode: {exc}") from exc
    return contract.summary()


@router.post("/integrations/opencode/clear-quota", tags=["Integrações"])
def clear_quota(request: Request) -> dict:
    backend = C(request).backends.get("deepseek")
    if not hasattr(backend, "clear_quota"):
        raise HTTPException(400, "Backend não suporta.")
    backend.clear_quota()
    return {"ok": True}
