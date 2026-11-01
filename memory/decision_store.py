"""Registro de decisões técnicas compartilhado entre os agentes."""
from __future__ import annotations

import json
from typing import Any

from database.repository import Repository, utc_now

VALID_STATUS = {"proposed", "accepted", "superseded", "rejected"}


class DecisionStore:
    def __init__(self, repo: Repository):
        self.repo = repo

    def add(self, title: str, decision: str, author: str, rationale: str = "", status: str = "accepted",
            tags: list[str] | None = None, workflow_id: str | None = None, subtask_id: str | None = None,
            supersedes: int | None = None) -> int:
        if status not in VALID_STATUS:
            raise ValueError(f"status inválido: {status}")
        if not title.strip() or not decision.strip():
            raise ValueError("title e decision são obrigatórios")
        now = utc_now()
        with self.repo.connect() as con:
            if subtask_id or workflow_id:
                dup = con.execute(
                    "SELECT id FROM decisions WHERE title=? AND author=? AND COALESCE(subtask_id,'')=COALESCE(?,'') "
                    "AND COALESCE(workflow_id,'')=COALESCE(?,'') AND status IN ('proposed','accepted')",
                    (title.strip()[:300], author, subtask_id, workflow_id),
                ).fetchone()
                if dup:
                    return int(dup["id"])
            cur = con.execute(
                """INSERT INTO decisions(created_at, updated_at, title, decision, rationale, author, status, tags_json,
                   workflow_id, subtask_id, supersedes) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (now, now, title.strip()[:300], decision.strip()[:4000], (rationale or "")[:4000], author, status,
                 json.dumps(tags or [], ensure_ascii=False), workflow_id, subtask_id, supersedes),
            )
            new_id = int(cur.lastrowid)
            if supersedes:
                con.execute("UPDATE decisions SET status='superseded', updated_at=? WHERE id=?", (now, supersedes))
        return new_id

    def set_status(self, decision_id: int, status: str) -> None:
        if status not in VALID_STATUS:
            raise ValueError(status)
        with self.repo.connect() as con:
            con.execute("UPDATE decisions SET status=?, updated_at=? WHERE id=?", (status, utc_now(), decision_id))

    def accept_for_subtask(self, subtask_id: str) -> None:
        with self.repo.connect() as con:
            con.execute("UPDATE decisions SET status='accepted', updated_at=? WHERE subtask_id=? AND status='proposed'",
                        (utc_now(), subtask_id))

    def reject_for_subtask(self, subtask_id: str) -> None:
        with self.repo.connect() as con:
            con.execute("UPDATE decisions SET status='rejected', updated_at=? WHERE subtask_id=? AND status='proposed'",
                        (utc_now(), subtask_id))

    def list(self, status: str | None = None, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if query:
            sql += " AND (title LIKE ? OR decision LIKE ? OR tags_json LIKE ?)"
            q = f"%{query}%"
            params += [q, q, q]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.repo.connect() as con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        for r in rows:
            r["tags"] = json.loads(r.pop("tags_json") or "[]")
        return rows

    def as_context(self, limit: int = 30) -> str:
        items = self.list(status="accepted", limit=limit) + self.list(status="proposed", limit=10)
        if not items:
            return "Nenhuma decisão técnica registrada no orquestrador."
        lines = []
        for d in items:
            lines.append(f"- [#{d['id']} {d['status']}] {d['title']}: {d['decision']}" + (f" (motivo: {d['rationale']})" if d.get("rationale") else ""))
        return "\n".join(lines)
