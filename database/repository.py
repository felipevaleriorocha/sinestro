"""Acesso ao SQLite (thread-safe, WAL, conexões sempre fechadas)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.logging_setup import redact
from database.models import migrate

ACTIVE_WORKFLOW_STATES = ("queued", "planning", "running", "delegating", "reviewing")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return redact(json.dumps(value, ensure_ascii=False, default=str))


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class Repository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        raw = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            raw.execute("PRAGMA journal_mode=WAL")  # precisa ser fora de transação
        finally:
            raw.close()
        with self.connect() as con:
            migrate(con)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=30000")
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    # ------------------------------------------------------------------ workflows
    def create_workflow(self, workflow_id: str, goal: str, mode: str, version: str) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO tasks(id, goal, status, created_at, updated_at, mode, phase, version) VALUES(?,?,?,?,?,?,?,?)",
                (workflow_id, goal, "queued", now, now, mode, "queued", version),
            )

    def update_workflow(self, workflow_id: str, **fields: Any) -> None:
        allowed = {"status", "phase", "result_json", "error", "context_hash", "context_sources_json", "plan_json", "cancel_requested"}
        sets, params = ["updated_at=?"], [utc_now()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            if key.endswith("_json"):
                value = _dumps(value)
            elif key == "error" and value is not None:
                value = redact(value)
            sets.append(f"{key}=?")
            params.append(value)
        params.append(workflow_id)
        with self.connect() as con:
            con.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)

    def get_workflow(self, workflow_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (workflow_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("result_json", "context_sources_json", "plan_json"):
            d[k.replace("_json", "")] = _loads(d.pop(k, None))
        d["cancel_requested"] = bool(d.get("cancel_requested"))
        return d

    def list_workflows(self, limit: int = 50, status: str | None = None) -> list[dict]:
        sql = "SELECT id, goal, status, phase, mode, created_at, updated_at, error, version FROM tasks"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def mark_interrupted_workflows(self) -> int:
        """Ao reiniciar: workflows que estavam ativos não têm mais processo; marcamos como interrompidos."""
        q = ",".join("?" * len(ACTIVE_WORKFLOW_STATES))
        with self.connect() as con:
            rows = con.execute(f"SELECT id FROM tasks WHERE status IN ({q})", ACTIVE_WORKFLOW_STATES).fetchall()
            for r in rows:
                con.execute(
                    "UPDATE tasks SET status='interrupted', updated_at=?, error=COALESCE(error, ?) WHERE id=?",
                    (utc_now(), "Serviço reiniciado durante a execução. Use POST /tasks/{id}/resume.", r["id"]),
                )
                con.execute(
                    "INSERT INTO task_events(task_id, created_at, agent, event, content, level) VALUES(?,?,?,?,?,?)",
                    (r["id"], utc_now(), "orchestrator", "interrupted", "Serviço reiniciado durante a execução.", "warning"),
                )
            con.execute(
                "UPDATE subtasks SET status='interrupted', updated_at=? WHERE status IN ('running','reviewing','delegated')",
                (utc_now(),),
            )
        return len(rows)

    # ------------------------------------------------------------------ events
    def add_event(self, workflow_id: str, agent: str, event: str, content: Any, level: str = "info", subtask_id: str | None = None) -> None:
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        with self.connect() as con:
            con.execute(
                "INSERT INTO task_events(task_id, created_at, agent, event, content, level, subtask_id) VALUES(?,?,?,?,?,?,?)",
                (workflow_id, utc_now(), agent, event, redact(text), level, subtask_id),
            )

    def list_events(self, workflow_id: str | None = None, level: str | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT id, task_id AS workflow_id, subtask_id, created_at, agent, event, level, content FROM task_events WHERE 1=1"
        params: list[Any] = []
        if workflow_id:
            sql += " AND task_id=?"
            params.append(workflow_id)
        if level:
            sql += " AND level=?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?" if not workflow_id else " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ subtasks
    def create_subtask(self, sub: dict) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO subtasks(id, workflow_id, seq, plan_id, title, agent, status, spec_json, files_json,
                   depends_on_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sub["id"], sub["workflow_id"], sub["seq"], sub.get("plan_id"), sub["title"], sub["agent"], "pending",
                    _dumps(sub["spec"]), _dumps(sub.get("files", [])), _dumps(sub.get("depends_on", [])), now, now,
                ),
            )

    def update_subtask(self, subtask_id: str, **fields: Any) -> None:
        allowed = {"status", "agent", "review_cycles", "a2a_context_id", "last_a2a_task_id", "result_json", "review_json", "error"}
        sets, params = ["updated_at=?"], [utc_now()]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            if key.endswith("_json"):
                value = _dumps(value)
            elif key == "error" and value is not None:
                value = redact(value)
            sets.append(f"{key}=?")
            params.append(value)
        params.append(subtask_id)
        with self.connect() as con:
            con.execute(f"UPDATE subtasks SET {', '.join(sets)} WHERE id=?", params)

    def get_subtask(self, subtask_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT workflow_id FROM subtasks WHERE id=?", (subtask_id,)).fetchone()
        if not row:
            return None
        return next((s for s in self.list_subtasks(row["workflow_id"]) if s["id"] == subtask_id), None)

    def list_subtasks(self, workflow_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM subtasks WHERE workflow_id=? ORDER BY seq", (workflow_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("spec_json", "files_json", "depends_on_json", "result_json", "review_json"):
                d[k.replace("_json", "")] = _loads(d.pop(k, None))
            out.append(d)
        return out

    # ------------------------------------------------------------------ A2A message history
    def add_message(self, **m: Any) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_messages(created_at, workflow_id, subtask_id, direction, sender, recipient, skill,
                   a2a_message_id, a2a_task_id, a2a_context_id, a2a_state, summary, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utc_now(), m.get("workflow_id"), m.get("subtask_id"), m["direction"], m["sender"], m["recipient"],
                    m.get("skill"), m.get("a2a_message_id"), m.get("a2a_task_id"), m.get("a2a_context_id"),
                    m.get("a2a_state"), redact(m.get("summary") or "")[:2000], _dumps(m.get("payload")),
                ),
            )

    def list_messages(self, workflow_id: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM agent_messages"
        params: list[Any] = []
        if workflow_id:
            sql += " WHERE workflow_id=?"
            params.append(workflow_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        for r in rows:
            r["payload"] = _loads(r.pop("payload_json", None))
        return list(reversed(rows)) if workflow_id else rows

    # ------------------------------------------------------------------ runs / sessions
    def add_run(self, **r: Any) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_runs(created_at, agent, model, a2a_task_id, workflow_id, subtask_id, skill, session_id,
                   status, duration_seconds, usage_json, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utc_now(), r["agent"], r.get("model"), r.get("a2a_task_id"), r.get("workflow_id"), r.get("subtask_id"),
                    r.get("skill"), r.get("session_id"), r["status"], r.get("duration_seconds"), _dumps(r.get("usage")),
                    redact(r["error"]) if r.get("error") else None,
                ),
            )

    def list_runs(self, agent: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM agent_runs"
        params: list[Any] = []
        if agent:
            sql += " WHERE agent=?"
            params.append(agent)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        for r in rows:
            r["usage"] = _loads(r.pop("usage_json", None))
        return rows

    def get_session(self, agent: str, context_key: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT session_id FROM agent_sessions WHERE agent=? AND context_key=?", (agent, context_key)).fetchone()
        return row["session_id"] if row else None

    def save_session(self, agent: str, context_key: str, session_id: str, model: str | None) -> None:
        now = utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO agent_sessions(agent, context_key, session_id, model, created_at, updated_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(agent, context_key) DO UPDATE SET session_id=excluded.session_id, model=excluded.model,
                   updated_at=excluded.updated_at""",
                (agent, context_key, session_id, model, now, now),
            )

    # ------------------------------------------------------------------ file changes
    def add_file_change(self, **c: Any) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO file_changes(created_at, workflow_id, subtask_id, agent, path, change_type, within_reservation,
                   source, git_head) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    utc_now(), c.get("workflow_id"), c.get("subtask_id"), c["agent"], c["path"], c["change_type"],
                    1 if c.get("within_reservation") else 0, c["source"], c.get("git_head"),
                ),
            )

    def list_file_changes(self, workflow_id: str | None = None, limit: int = 300) -> list[dict]:
        sql = "SELECT * FROM file_changes"
        params: list[Any] = []
        if workflow_id:
            sql += " WHERE workflow_id=?"
            params.append(workflow_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
