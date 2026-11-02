"""Acesso ao SQLite (thread-safe, WAL, conexões sempre fechadas)."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from core.logging_setup import redact
from database.models import backup_before_migration, migrate

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
        self.backup_path = backup_before_migration(self.db_path)
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
        allowed = {"status", "phase", "result_json", "error", "context_hash", "context_sources_json", "plan_json", "cancel_requested",
                   "planned_by", "planned_by_fallback", "metrics_json", "mode", "final_review_state", "final_review_json",
                   "dependency_analysis_json", "plan_reformulations"}
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
        for k in ("result_json", "context_sources_json", "plan_json", "metrics_json", "final_review_json", "dependency_analysis_json"):
            d[k.replace("_json", "")] = _loads(d.pop(k, None))
        d["cancel_requested"] = bool(d.get("cancel_requested"))
        d["planned_by_fallback"] = bool(d.get("planned_by_fallback"))
        return d

    def list_workflows(self, limit: int = 50, status: str | None = None) -> list[dict]:
        sql = "SELECT id, goal, status, phase, mode, created_at, updated_at, error, version, final_review_state FROM tasks"
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
            # Interrompida durante a revisão e com relatório salvo: retoma direto na revisão (não reimplementa).
            con.execute(
                "UPDATE subtasks SET status='awaiting_review', updated_at=? WHERE status='reviewing' AND result_json IS NOT NULL",
                (utc_now(),),
            )
            con.execute(
                "UPDATE subtasks SET status='interrupted', updated_at=? WHERE status IN ('running','reviewing','checking','delegated')",
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
                   depends_on_json, created_at, updated_at, assigned_to) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sub["id"], sub["workflow_id"], sub["seq"], sub.get("plan_id"), sub["title"], sub["agent"], "pending",
                    _dumps(sub["spec"]), _dumps(sub.get("files", [])), _dumps(sub.get("depends_on", [])), now, now, sub["agent"],
                ),
            )

    def update_subtask(self, subtask_id: str, **fields: Any) -> None:
        allowed = {"status", "agent", "review_cycles", "a2a_context_id", "last_a2a_task_id", "result_json", "review_json", "error",
                   "assigned_to", "reviewer", "assignment_reason", "assignment_json", "checks_json", "escalation_json",
                   "human_decision_json", "check_fix_attempts", "impl_attempts", "dependency_class", "execution_group",
                   "dependency_reasons_json", "resources_json", "started_at", "finished_at"}
        if fields.get("assigned_to") and fields.get("reviewer") and fields["assigned_to"] == fields["reviewer"]:
            raise ValueError("reviewer não pode ser igual a assigned_to (autorrevisão proibida).")
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
            for k in ("spec_json", "files_json", "depends_on_json", "result_json", "review_json", "assignment_json", "checks_json",
                      "escalation_json", "human_decision_json", "dependency_reasons_json", "resources_json"):
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

    def runs_for_workflow(self, workflow_id: str) -> list[dict]:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT agent, skill, status, duration_seconds, model, usage_json FROM agent_runs WHERE workflow_id=? ORDER BY id",
                (workflow_id,),
            ).fetchall()]
        for r in rows:
            r["usage"] = _loads(r.pop("usage_json", None))
        return rows

    def get_cost_summary(self, workflow_id: str | None = None) -> dict[str, Any]:
        """Calcula o resumo agregado de execuções, contagem de tokens e custos estimados."""
        sql = "SELECT agent, model, status, duration_seconds, usage_json FROM agent_runs"
        params: list[Any] = []
        if workflow_id:
            sql += " WHERE workflow_id=?"
            params.append(workflow_id)

        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]

        by_agent: dict[str, dict[str, Any]] = {}
        total_runs = len(rows)
        total_tokens_in = 0
        total_tokens_out = 0
        total_estimated_usd = 0.0

        for r in rows:
            agent = r["agent"]
            model = r.get("model") or "unknown"
            usage = _loads(r.get("usage_json")) or {}
            tok = usage.get("tokens") or {}
            t_in = tok.get("prompt") or tok.get("input") or 0
            t_out = tok.get("completion") or tok.get("output") or 0

            rate_in = 1.25 if ("gemini" in agent.lower() or "gemini" in model.lower()) else 0.27
            rate_out = 5.00 if ("gemini" in agent.lower() or "gemini" in model.lower()) else 1.10
            cost = float(usage.get("cost") if usage.get("cost") is not None else ((t_in * rate_in + t_out * rate_out) / 1_000_000.0))

            total_tokens_in += t_in
            total_tokens_out += t_out
            total_estimated_usd += cost

            ag_stat = by_agent.setdefault(agent, {
                "runs": 0, "successful": 0, "failed": 0,
                "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0,
                "models_used": set(),
            })
            ag_stat["runs"] += 1
            if r["status"] == "completed":
                ag_stat["successful"] += 1
            else:
                ag_stat["failed"] += 1
            ag_stat["input_tokens"] += t_in
            ag_stat["output_tokens"] += t_out
            ag_stat["estimated_cost_usd"] = round(ag_stat["estimated_cost_usd"] + cost, 6)
            ag_stat["models_used"].add(model)

        for ag in by_agent.values():
            ag["models_used"] = sorted(ag["models_used"])

        return {
            "workflow_id": workflow_id,
            "total_runs": total_runs,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "total_estimated_usd": round(total_estimated_usd, 6),
            "by_agent": by_agent,
        }

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

    # ------------------------------------------------------------------ dependency analysis / stages
    def save_dependency_analysis(self, workflow_id: str, analysis: dict, reformulations: int = 0) -> None:
        self.update_workflow(workflow_id, dependency_analysis_json=analysis, plan_reformulations=reformulations)

    def apply_dependency_analysis(self, workflow_id: str, analysis: dict) -> None:
        groups = {task_id: index for index, group in enumerate(analysis.get("groups") or [], 1) for task_id in group}
        reasons: dict[str, list[str]] = {}
        for rel in analysis.get("relations") or []:
            for task_id in (rel.get("source"), rel.get("target")):
                if task_id:
                    reasons.setdefault(task_id, []).extend(rel.get("reasons") or [])
        for sub in self.list_subtasks(workflow_id):
            plan_id = sub.get("plan_id")
            resources = (sub.get("spec") or {}).get("resources") or []
            self.update_subtask(sub["id"], dependency_class=(analysis.get("classifications") or {}).get(plan_id),
                                execution_group=groups.get(plan_id), dependency_reasons_json=sorted(set(reasons.get(plan_id, []))),
                                resources_json=resources)

    def start_stage(self, workflow_id: str, stage: str, *, subtask_id: str | None = None, agent: str | None = None,
                    execution_group: int | None = None, detail: Any = None) -> int:
        with self.connect() as con:
            cur = con.execute("INSERT INTO stage_log(workflow_id, subtask_id, execution_group, stage, agent, status, started_at, detail_json) "
                              "VALUES(?,?,?,?,?,'running',?,?)",
                              (workflow_id, subtask_id, execution_group, stage, agent, utc_now(), _dumps(detail)))
            return int(cur.lastrowid)

    def finish_stage(self, stage_id: int, status: str, detail: Any = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE stage_log SET status=?, finished_at=?, detail_json=COALESCE(?, detail_json) WHERE id=?",
                        (status, utc_now(), _dumps(detail), stage_id))

    def list_stages(self, workflow_id: str) -> list[dict]:
        with self.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM stage_log WHERE workflow_id=? ORDER BY id", (workflow_id,)).fetchall()]
        for row in rows:
            row["detail"] = _loads(row.pop("detail_json", None))
        return rows

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

    # ------------------------------------------------------------------ routing & retention (fase 4)
    def record_subtask_outcome(
        self,
        agent: str,
        category: str,
        first_pass: bool,
        review_cycles: int,
        duration_seconds: float,
    ) -> None:
        """Registra o desfecho de uma subtarefa no placar consolidado de roteamento."""
        cat = (category or "other").lower().strip()
        now = utc_now()
        with self.connect() as con:
            con.execute(
                """INSERT INTO routing_stats(
                    agent, category, samples_count, first_pass_count,
                    total_review_cycles, total_duration_seconds, last_interaction_at
                ) VALUES(?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(agent, category) DO UPDATE SET
                    samples_count = routing_stats.samples_count + 1,
                    first_pass_count = routing_stats.first_pass_count + excluded.first_pass_count,
                    total_review_cycles = routing_stats.total_review_cycles + excluded.total_review_cycles,
                    total_duration_seconds = routing_stats.total_duration_seconds + excluded.total_duration_seconds,
                    last_interaction_at = excluded.last_interaction_at""",
                (
                    agent.lower().strip(),
                    cat,
                    1 if first_pass else 0,
                    max(0, int(review_cycles)),
                    max(0.0, float(duration_seconds)),
                    now,
                ),
            )

    def get_routing_stats(self, category: str | None = None, agent: str | None = None) -> list[dict]:
        """Consulta as estatísticas agregadas de desempenho por agente e categoria."""
        sql = """SELECT agent, category, samples_count, first_pass_count, total_review_cycles,
                        total_duration_seconds, last_interaction_at FROM routing_stats WHERE 1=1"""
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category.lower().strip())
        if agent:
            sql += " AND agent = ?"
            params.append(agent.lower().strip())
        sql += " ORDER BY samples_count DESC, first_pass_count DESC"
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
            out = []
            for r in rows:
                samples = r["samples_count"] or 1
                out.append({
                    "agent": r["agent"],
                    "category": r["category"],
                    "samples_count": r["samples_count"],
                    "first_pass_count": r["first_pass_count"],
                    "first_pass_rate": round(r["first_pass_count"] / samples, 3),
                    "avg_review_cycles": round(r["total_review_cycles"] / samples, 2),
                    "avg_duration_seconds": round(r["total_duration_seconds"] / samples, 2),
                    "last_interaction_at": r["last_interaction_at"],
                })
            return out

    def touch_workflow(self, workflow_id: str) -> None:
        """Atualiza updated_at do workflow, renovando sua janela de 30 dias de retenção."""
        now = utc_now()
        with self.connect() as con:
            con.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, workflow_id))

    def prune_inactive_workflows(self, days: int = 30) -> int:
        """Remove workflows cuja última interação (updated_at) ocorreu há mais de `days` dias.
        
        Preserva workflows que tiveram qualquer interação (touch) dentro dos últimos `days` dias.
        Workflows em estados ativos (queued, running, planning, etc.) nunca são expurgados.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.connect() as con:
            active_placeholders = ",".join("?" for _ in ACTIVE_WORKFLOW_STATES)
            candidates = con.execute(
                f"SELECT id FROM tasks WHERE updated_at < ? AND status NOT IN ({active_placeholders})",
                [cutoff, *ACTIVE_WORKFLOW_STATES],
            ).fetchall()
            if not candidates:
                return 0
            wf_ids = [r["id"] for r in candidates]
            id_placeholders = ",".join("?" for _ in wf_ids)

            con.execute(f"DELETE FROM task_events WHERE task_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM agent_messages WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM agent_runs WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM stage_log WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM file_changes WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM subtasks WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM decisions WHERE workflow_id IN ({id_placeholders})", wf_ids)
            con.execute(f"DELETE FROM tasks WHERE id IN ({id_placeholders})", wf_ids)
            return len(wf_ids)

