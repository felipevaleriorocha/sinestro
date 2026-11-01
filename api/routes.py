"""Rotas REST do orquestrador e do painel."""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from communication.a2a_server import agent_base_path, card_to_dict
from api.container import update_auto_approve_permissions
from orchestrator.assignment import other
from orchestrator.dependency_analyzer import DependencyAnalyzer
from orchestrator.schemas import AssignmentDecision
from orchestrator.task_manager import FR, ST, WF, final_status

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


class FinalReviewDecision(BaseModel):
    action: Literal["accept", "reject"]
    note: str = Field(default="", max_length=4000)


class HumanDecision(BaseModel):
    action: Literal["approve", "retry", "reassign", "reject"]
    note: str = Field(default="", max_length=4000)


class AutoApproveRequest(BaseModel):
    enabled: bool



def C(request: Request):
    return request.app.state.container


@router.get("/health", tags=["Sistema"])
async def health(request: Request) -> dict[str, Any]:
    c = C(request)
    return {"ok": True, "version": "0.5.1", "mock_mode": c.mock_mode, **c.settings.public_summary(),
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
        "final_review_state": wf.get("final_review_state"), "final_review": wf.get("final_review"),
        "planned_by": wf.get("planned_by"), "planned_by_fallback": wf.get("planned_by_fallback"), "metrics": wf.get("metrics"),
        "dependency_analysis": wf.get("dependency_analysis"), "plan_reformulations": wf.get("plan_reformulations", 0),
        "stages": c.repo.list_stages(task_id),
        "running": c.coordinator.is_running(task_id),
        "subtasks": c.tasks.subtasks(task_id),
        "events": [{k: e[k] for k in ("created_at", "agent", "event", "level", "content", "subtask_id")} for e in events],
    }



@router.post("/tasks/{task_id}/analyze", tags=["Tarefas"])
def analyze_task(task_id: str, request: Request) -> dict:
    """Reanalisa deterministicamente o plano persistido. Não chama modelos nem executa subtarefas."""
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    plan = wf.get("plan") or {}
    if not plan.get("tasks"):
        raise HTTPException(409, "Workflow ainda não possui plano.")
    result = DependencyAnalyzer(c.settings.project_root, policy_path=c.settings.dependency_policy_file).analyze(plan["tasks"]).to_dict()
    return result

@router.get("/tasks/{task_id}/report", tags=["Tarefas"])
def task_report(task_id: str, request: Request) -> dict:
    """Relatório operacional consolidado, incluindo dependências e execução real."""
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    return {
        "id": task_id, "goal": wf["goal"], "status": wf["status"],
        "dependency_analysis": wf.get("dependency_analysis"),
        "subtasks": c.tasks.subtasks(task_id),
        "stages": c.repo.list_stages(task_id),
        "file_changes": c.repo.list_file_changes(task_id),
        "final_review": wf.get("final_review"), "metrics": wf.get("metrics"),
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
    pending = [s["id"] for s in c.tasks.subtasks(task_id) if s["status"] == ST.NEEDS_HUMAN]
    if pending:
        raise HTTPException(409, {"detail": "Há subtarefas aguardando decisão humana. Use POST /tasks/{id}/subtasks/{sid}/decision.",
                                  "subtasks": pending})
    if wf["status"] == WF.PLANNED:
        c.repo.update_workflow(task_id, status=WF.QUEUED, mode="full")
    c.coordinator.start(task_id, resume=True)
    return {"id": task_id, "status": "resuming"}


@router.post("/tasks/{task_id}/subtasks/{subtask_id}/decision", tags=["Tarefas"])
def human_decision(task_id: str, subtask_id: str, body: HumanDecision, request: Request) -> dict:
    """Decisão humana para subtarefa em `needs_human`. Depois, chame POST /tasks/{id}/resume."""
    c = C(request)
    sub = c.repo.get_subtask(subtask_id)
    if not sub or sub["workflow_id"] != task_id:
        raise HTTPException(404, "Subtarefa não encontrada.")
    if sub["status"] != ST.NEEDS_HUMAN:
        raise HTTPException(409, f"Subtarefa não aguarda decisão (status {sub['status']}).")
    if c.coordinator.is_running(task_id):
        raise HTTPException(409, "Workflow em execução.")
    decision = {"action": body.action, "note": body.note, "consumed": False}
    reset = {"review_cycles": 0, "check_fix_attempts": 0, "impl_attempts": 0}
    if body.action == "approve":
        if sub.get("result") is None:
            decision["allow_permissions"] = True
            c.repo.update_subtask(subtask_id, status=ST.PENDING, human_decision_json=decision, error=None)
        else:
            c.repo.update_subtask(subtask_id, status=ST.APPROVED, human_decision_json=decision, error=None)
            c.decisions.accept_for_subtask(subtask_id)
    elif body.action == "reject":
        c.repo.update_subtask(subtask_id, status=ST.REJECTED, human_decision_json=decision, error=f"Rejeitada por decisão humana: {body.note}")
        c.decisions.reject_for_subtask(subtask_id)
    elif body.action == "retry":
        c.repo.update_subtask(subtask_id, status=ST.PENDING, human_decision_json=decision, error=None, **reset)
    else:  # reassign: troca quem implementa e quem revisa
        new_impl = other(sub.get("assigned_to") or sub["agent"])
        d = AssignmentDecision(task_id=sub["plan_id"], assigned_to=new_impl, reviewer=other(new_impl), decided_by="human",
                               proposed_to=sub.get("assigned_to"), reason=f"Reatribuída por decisão humana: {body.note}"[:400])
        c.repo.update_subtask(subtask_id, status=ST.PENDING, human_decision_json=decision, error=None, agent=new_impl,
                              assigned_to=new_impl, reviewer=d.reviewer, assignment_reason=f"[human] {d.reason}",
                              assignment_json=d.model_dump(), a2a_context_id=None, **reset)
    c.tasks.event(task_id, "user", f"human_decision_{body.action}", {"subtask": subtask_id, "note": body.note}, subtask_id=subtask_id)
    return {"subtask": subtask_id, "action": body.action, "next": f"POST /tasks/{task_id}/resume"}


@router.post("/tasks/{task_id}/final-review/decision", tags=["Tarefas"])
def final_review_decision(task_id: str, body: FinalReviewDecision, request: Request) -> dict:
    """Decisão humana sobre parecer NEGATIVO/não verificável da revisão final (sem ciclo automático de correção).
    accept -> accepted_by_human (completed); reject -> rejected_by_human (failed).
    Para pedir uma NOVA revisão final (ex.: depois de corrigir à mão), use POST /tasks/{id}/resume."""
    c = C(request)
    wf = c.repo.get_workflow(task_id)
    if not wf:
        raise HTTPException(404, "Tarefa não encontrada.")
    if c.coordinator.is_running(task_id):
        raise HTTPException(409, "Workflow em execução.")
    if wf["status"] != WF.NEEDS_HUMAN or wf.get("final_review_state") not in FR.NEEDS_HUMAN:
        raise HTTPException(409, f"Não há parecer de revisão final aguardando decisão (status {wf['status']}, "
                                 f"final_review_state {wf.get('final_review_state')}).")
    state = FR.ACCEPTED_BY_HUMAN if body.action == "accept" else FR.REJECTED_BY_HUMAN
    fr = dict(wf.get("final_review") or {})
    fr["human_decision"] = {"action": body.action, "note": body.note, "previous_state": wf.get("final_review_state")}
    fr["state"] = state
    subs = c.tasks.subtasks(task_id)
    status = final_status(subs, False, False, state)
    c.repo.update_workflow(task_id, final_review_state=state, final_review_json=fr,
                           error=None if status == WF.COMPLETED else f"Revisão final rejeitada por decisão humana: {body.note}")
    c.tasks.set_status(task_id, status, phase="concluded")
    c.tasks.event(task_id, "user", f"final_review_{state}", {"note": body.note, "previous_state": fr["human_decision"]["previous_state"]},
                  "warning")
    return {"id": task_id, "final_review_state": state, "status": status}


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
def release_expired(request: Request, force: bool = Query(True)) -> dict:
    return {"released": C(request).locks.release_expired(force=force)}


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


@router.get("/context/search", tags=["Memória"])
async def context_search(request: Request, q: str = Query(..., min_length=2), top_k: int = Query(5, ge=1, le=20)) -> list[dict]:
    c = C(request)
    results = await asyncio.to_thread(c.context.search_relevant, q, top_k)
    return [r.to_dict() for r in results]


@router.post("/context/index", tags=["Memória"])
async def context_index(request: Request, refresh: bool = False) -> dict:
    c = C(request)
    count = await asyncio.to_thread(c.context.index_workspace, refresh=refresh)
    return {"indexed_chunks": count, "total_chunks": c.context.vector_store.count()}


@router.get("/metrics/costs", tags=["Sistema"])
def metrics_costs(request: Request, workflow_id: str | None = None) -> dict:
    return C(request).repo.get_cost_summary(workflow_id)


@router.get("/providers", tags=["Provedores"])
def list_providers(request: Request) -> list[dict]:
    c = C(request)
    cap_reg = c.capability_registry
    if not cap_reg:
        return []
    models = cap_reg.describe()
    out = []
    for p in cap_reg.list_providers():
        out.append({
            "id": p.id,
            "name": p.name,
            "kind": getattr(p, "kind", "provider"),
            "description": getattr(p, "description", ""),
            "models": [m for m in models if m["provider_id"] == p.id],
        })
    return out


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
    return clear_agent_quota("deepseek", request)


@router.post("/agents/{name}/clear-quota", tags=["Agentes"])
def clear_agent_quota(name: str, request: Request) -> dict:
    backend = C(request).backends.get(name)
    if backend is None or not hasattr(backend, "clear_quota"):
        raise HTTPException(400, "Agente desconhecido ou sem controle de cota.")
    backend.clear_quota()
    return {"ok": True, "agent": name}


@router.get("/settings/auto-approve", tags=["Sistema"])
def get_auto_approve_setting(request: Request) -> dict[str, Any]:
    c = C(request)
    return {"enabled": bool(c.settings.agy_auto_approve_permissions)}


@router.post("/settings/auto-approve", tags=["Sistema"])
def update_auto_approve_setting(body: AutoApproveRequest, request: Request) -> dict[str, Any]:
    c = C(request)
    new_settings = update_auto_approve_permissions(c, body.enabled)
    return {"ok": True, "enabled": new_settings.agy_auto_approve_permissions}


