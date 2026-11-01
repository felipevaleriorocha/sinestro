from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", "")).expanduser()
OPENCODE_URL = os.getenv("OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
OPENCODE_MODEL = os.getenv("OPENCODE_MODEL", "").strip()
OPENCODE_AGENT = os.getenv("OPENCODE_AGENT", "build").strip()
OPENCODE_VARIANT = os.getenv("OPENCODE_VARIANT", "high").strip()
OPENCODE_SERVER_USERNAME = os.getenv("OPENCODE_SERVER_USERNAME", "").strip()
OPENCODE_SERVER_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "").strip()
AGY_BIN = os.getenv("AGY_BIN", "agy").strip()
AGY_MODEL = os.getenv("AGY_MODEL", "").strip()
AGY_EFFORT = os.getenv("AGY_EFFORT", "high").strip()
DB_PATH = BASE_DIR / "orchestrator.db"

app = FastAPI(title="Roblox Multi-Agent Orchestrator", version="0.1.0")
project_lock = asyncio.Lock()  # v0.1 serializes work to prevent shared-file write collisions.


class TaskRequest(BaseModel):
    goal: str = Field(min_length=8, max_length=8000)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result_json TEXT,
                error TEXT
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                agent TEXT NOT NULL,
                event TEXT NOT NULL,
                content TEXT NOT NULL
            )"""
        )


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def update_task(task_id: str, status: str, result: Any | None = None, error: str | None = None) -> None:
    with db() as con:
        con.execute(
            "UPDATE tasks SET status=?, updated_at=?, result_json=?, error=? WHERE id=?",
            (status, utc_now(), json.dumps(result, ensure_ascii=False) if result is not None else None, error, task_id),
        )


def add_event(task_id: str, agent: str, event: str, content: str) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO task_events(task_id, created_at, agent, event, content) VALUES(?,?,?,?,?)",
            (task_id, utc_now(), agent, event, content),
        )


def parse_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # Recover a JSON object if the model wrapped it in explanatory text.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("O agente não retornou um objeto JSON válido.")


def run_agy(prompt: str, conversation_id: str | None = None) -> dict[str, Any]:
    if not PROJECT_ROOT.is_dir():
        raise RuntimeError("PROJECT_ROOT não existe. Confira o caminho no arquivo .env.")

    command = [AGY_BIN, "-p", prompt, "--output-format", "json", "--effort", AGY_EFFORT, "--print-timeout", "20m"]
    if AGY_MODEL:
        command.extend(["--model", AGY_MODEL])
    if conversation_id:
        command.extend(["--conversation", conversation_id])

    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1250,
        check=False,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Antigravity CLI falhou (código {completed.returncode}): {details[-2500:]}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Antigravity não retornou JSON válido: {completed.stdout[-1500:]}") from exc

    status = payload.get("status", "SUCCESS")
    if status != "SUCCESS":
        raise RuntimeError(f"Antigravity terminou com status {status}: {payload.get('error', payload.get('response', 'sem detalhes'))}")
    response = payload.get("response", "")
    if not response:
        raise RuntimeError("Antigravity retornou uma resposta vazia.")
    return {"response": response, "conversation_id": payload.get("conversation_id"), "usage": payload.get("usage")}


def opencode_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    """Call the OpenCode 2.x HTTP API using optional HTTP Basic authentication."""
    url = f"{OPENCODE_URL}{path}"
    auth = None
    if OPENCODE_SERVER_USERNAME or OPENCODE_SERVER_PASSWORD:
        if not (OPENCODE_SERVER_USERNAME and OPENCODE_SERVER_PASSWORD):
            raise RuntimeError("Configure OPENCODE_SERVER_USERNAME e OPENCODE_SERVER_PASSWORD juntos no .env.")
        auth = (OPENCODE_SERVER_USERNAME, OPENCODE_SERVER_PASSWORD)

    response = requests.request(
        method,
        url,
        json=payload,
        auth=auth,
        timeout=(10, 1800),
        headers={"Accept": "application/json"},
    )
    if not response.ok:
        content_type = response.headers.get("Content-Type", "")
        detail = response.text[:2000]
        if "text/html" in content_type:
            detail = "O OpenCode retornou HTML em vez de JSON. Confira a rota /api/ e a autenticação."
        raise RuntimeError(f"OpenCode HTTP {response.status_code} em {path}: {detail}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"OpenCode não retornou JSON em {path}: {response.text[:500]}") from exc


def parse_opencode_model() -> dict[str, str]:
    """OpenCode 2.x Model.Ref: {providerID, id, variant}."""
    if not OPENCODE_MODEL:
        raise RuntimeError("Defina OPENCODE_MODEL=opencode/deepseek-v4.1-flash-free no .env.")
    if "/" not in OPENCODE_MODEL:
        raise RuntimeError("OPENCODE_MODEL deve estar no formato provider/model, por exemplo opencode/deepseek-v4.1-flash-free.")
    provider_id, model_id = OPENCODE_MODEL.split("/", 1)
    model_ref = {"providerID": provider_id, "id": model_id}
    if OPENCODE_VARIANT:
        model_ref["variant"] = OPENCODE_VARIANT
    return model_ref


def create_opencode_session(title: str) -> str:
    if not PROJECT_ROOT.is_dir():
        raise RuntimeError("PROJECT_ROOT não existe. Confira o .env.")

    payload: dict[str, Any] = {
        "title": title[:100],
        "model": parse_opencode_model(),
        "location": {"directory": str(PROJECT_ROOT.resolve())},
    }
    if OPENCODE_AGENT:
        payload["agent"] = OPENCODE_AGENT

    result = opencode_request("POST", "/api/session", payload)
    session = result.get("data", result) if isinstance(result, dict) else {}
    session_id = session.get("id") or session.get("sessionID")
    if not session_id:
        raise RuntimeError(f"OpenCode não retornou ID de sessão: {result}")
    return session_id


def run_deepseek(session_id: str, prompt: str) -> str:
    # OpenCode 2.x accepts plain text in /prompt, then exposes completion via /wait + /message.
    opencode_request("POST", f"/api/session/{session_id}/prompt", {"text": prompt})
    opencode_request("POST", f"/api/experimental/session/{session_id}/wait")

    result = opencode_request(
        "GET",
        f"/api/session/{session_id}/message?limit=100&order=desc&type=assistant",
    )
    messages = result.get("data", []) if isinstance(result, dict) else []
    for message in messages:
        if message.get("type") != "assistant":
            continue
        content = message.get("content", [])
        text_parts = [
            part.get("text", "")
            for part in content
            if part.get("type") == "text" and part.get("text")
        ]
        answer = "\n".join(text_parts).strip()
        if answer:
            return answer

    raise RuntimeError(f"OpenCode concluiu sem retornar texto do DeepSeek. Resposta da API: {json.dumps(result, ensure_ascii=False)[:2000]}")


def project_context() -> str:
    readme = PROJECT_ROOT / "README.md"
    agents = PROJECT_ROOT / "AGENTS.md"
    pieces = []
    for file in (readme, agents):
        if file.is_file():
            try:
                pieces.append(f"--- {file.name} ---\n{file.read_text(encoding='utf-8')[:12000]}")
            except OSError:
                pass
    return "\n\n".join(pieces) if pieces else "Não há README.md ou AGENTS.md na raiz. Inspecione o projeto antes de alterar arquivos."


PLANNER_SYSTEM = """Você é o Tech Lead de um projeto Roblox. Planeje uma solicitação em subtarefas executáveis.
Responda SOMENTE JSON válido neste formato:
{
  "summary": "resumo do plano",
  "tasks": [
    {
      "id": "T1",
      "agent": "deepseek" ou "gemini",
      "title": "título curto",
      "instructions": "instruções concretas e verificáveis",
      "files": ["caminhos relativos esperados"]
    }
  ]
}
Regras:
- Crie de 1 a 5 tarefas, em ordem de execução.
- Use deepseek para programação Luau, lógica, debugging e sistemas; use gemini para arquitetura, revisão, UI/visual e tarefas que exijam análise ampla. Pode inverter se houver motivo claro.
- Evite duas tarefas que modifiquem os mesmos arquivos.
- Não inclua tarefas vagas como 'fazer tudo'.
- Considere o contexto real do projeto fornecido.
- Não edite arquivos nesta etapa; apenas planeje.
"""


async def process_task(task_id: str, goal: str) -> None:
    async with project_lock:
        update_task(task_id, "planning")
        events: list[dict[str, Any]] = []
        gemini_conversation: str | None = None
        try:
            if not PROJECT_ROOT.is_dir():
                raise RuntimeError("PROJECT_ROOT não existe. Configure o caminho absoluto no .env.")

            context = await asyncio.to_thread(project_context)
            planner_prompt = (
                f"{PLANNER_SYSTEM}\n\nSOLICITAÇÃO DO USUÁRIO:\n{goal}\n\n"
                f"CONTEXTO DO PROJETO:\n{context}\n\n"
                "Antes de escolher as tarefas, considere a estrutura real do workspace. Retorne somente o JSON."
            )
            plan_run = await asyncio.to_thread(run_agy, planner_prompt)
            gemini_conversation = plan_run.get("conversation_id")
            plan = parse_json_from_text(plan_run["response"])
            tasks = plan.get("tasks", [])
            if not isinstance(tasks, list) or not tasks:
                raise RuntimeError("O Gemini não retornou tarefas no plano.")
            if len(tasks) > 5:
                tasks = tasks[:5]

            events.append({"agent": "gemini", "event": "plan", "content": json.dumps(plan, ensure_ascii=False)})
            add_event(task_id, "gemini", "plan", json.dumps(plan, ensure_ascii=False))

            opencode_session = await asyncio.to_thread(create_opencode_session, f"Orchestrator {task_id}")
            completed_summaries: list[str] = []
            update_task(task_id, "running", {"plan": plan, "events": events})

            for index, item in enumerate(tasks, start=1):
                agent = str(item.get("agent", "deepseek")).lower()
                if agent not in {"deepseek", "gemini"}:
                    agent = "deepseek"
                title = str(item.get("title", f"Tarefa {index}"))
                instructions = str(item.get("instructions", "Execute a tarefa conforme a solicitação principal."))
                files = item.get("files", [])
                add_event(task_id, agent, "task_started", title)

                execution_prompt = (
                    "Você é um agente de desenvolvimento colaborando em um projeto Roblox.\n"
                    f"OBJETIVO GERAL: {goal}\n"
                    f"SUA SUBTAREFA ({index}/{len(tasks)}): {title}\n"
                    f"INSTRUÇÕES: {instructions}\n"
                    f"ARQUIVOS PREVISTOS: {json.dumps(files, ensure_ascii=False)}\n"
                    f"RESULTADOS ANTERIORES: {json.dumps(completed_summaries, ensure_ascii=False)}\n\n"
                    "Regras: inspecione antes de editar; respeite os padrões existentes; não reverta mudanças de outro agente; "
                    "não altere arquivos fora do escopo sem necessidade; não invente APIs do Roblox; ao terminar, informe arquivos alterados, "
                    "decisões, testes executados e pendências. Não declare testes como aprovados se não os executou."
                )

                if agent == "deepseek":
                    answer = await asyncio.to_thread(run_deepseek, opencode_session, execution_prompt)
                else:
                    gemini_run = await asyncio.to_thread(run_agy, execution_prompt, gemini_conversation)
                    gemini_conversation = gemini_run.get("conversation_id") or gemini_conversation
                    answer = gemini_run["response"]

                completed_summaries.append(f"{title} ({agent}): {answer[:7000]}")
                events.append({"agent": agent, "event": "task_completed", "title": title, "content": answer})
                add_event(task_id, agent, "task_completed", f"{title}\n{answer}")

            # Gemini reviews the shared workspace and receives DeepSeek's implementation summary.
            review_prompt = (
                "Atue como revisor técnico do projeto Roblox. Revise o estado atual dos arquivos no workspace e os resultados abaixo. "
                "Não faça alterações ainda. Responda SOMENTE JSON válido: "
                '{"approved": true/false, "findings": ["..."], "recommended_fixes": ["..."]}. '
                "Se não puder verificar algo, diga isso em findings.\n\n"
                f"SOLICITAÇÃO ORIGINAL: {goal}\n\nRESULTADOS DOS AGENTES:\n{json.dumps(completed_summaries, ensure_ascii=False)}"
            )
            review_run = await asyncio.to_thread(run_agy, review_prompt, gemini_conversation)
            review_text = review_run["response"]
            try:
                review = parse_json_from_text(review_text)
            except Exception:
                review = {"approved": False, "findings": ["O revisor não retornou JSON estruturado."], "raw_response": review_text}
            events.append({"agent": "gemini", "event": "review", "content": review})
            add_event(task_id, "gemini", "review", json.dumps(review, ensure_ascii=False))

            final_result = {"goal": goal, "plan": plan, "execution": events, "review": review}
            update_task(task_id, "completed", final_result)
        except Exception as exc:
            update_task(task_id, "failed", {"events": events}, str(exc))
            add_event(task_id, "orchestrator", "error", str(exc))


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "project_root_exists": PROJECT_ROOT.is_dir(),
        "project_root": str(PROJECT_ROOT),
        "opencode_url": OPENCODE_URL,
        "opencode_model": OPENCODE_MODEL,
        "agy_binary": AGY_BIN,
    }


@app.post("/tasks", status_code=202)
async def create_task(request: TaskRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    task_id = str(uuid.uuid4())
    with db() as con:
        con.execute(
            "INSERT INTO tasks(id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_id, request.goal, "queued", utc_now(), utc_now()),
        )
    background_tasks.add_task(process_task, task_id, request.goal)
    return {"id": task_id, "status": "queued", "status_url": f"/tasks/{task_id}"}


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    with db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
        events = con.execute(
            "SELECT created_at, agent, event, content FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    return {
        "id": row["id"],
        "goal": row["goal"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
        "events": [dict(e) for e in events],
    }
