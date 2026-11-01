"""Reserva de arquivos/recursos com TTL, detecção de conflito e histórico.

Recursos:
  file:<caminho relativo normalizado>   arquivos do workspace
  mcp:<servidor>:<alvo>                 elementos manipulados via MCP (ex.: mcp:roblox_studio:Workspace/Map)
A aquisição é atômica (tudo ou nada) dentro de uma transação SQLite BEGIN IMMEDIATE.
"""
from __future__ import annotations

import asyncio
import os
import posixpath
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.repository import Repository, utc_now


class LockConflictError(RuntimeError):
    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        desc = ", ".join(f"{c['resource']} (com {c['owner']} até {c['expires_at']})" for c in conflicts)
        super().__init__(f"Recursos já reservados: {desc}")


class PathOutsideWorkspaceError(ValueError):
    pass


@dataclass
class LockGrant:
    resources: list[str]
    owner: str
    expires_at: str


def normalize_file(project_root: Path, path: str) -> str:
    """Normaliza para caminho relativo com '/', minúsculo no Windows; recusa caminhos fora do workspace."""
    if not path or not str(path).strip():
        raise PathOutsideWorkspaceError("Caminho vazio.")
    raw = str(path).strip().replace("\\", "/")
    root = Path(project_root).resolve()
    p = Path(raw)
    if p.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        try:
            rel = Path(os.path.abspath(raw)).resolve().relative_to(root)
        except ValueError as exc:
            raise PathOutsideWorkspaceError(f"Caminho fora do workspace: {path}") from exc
        raw = rel.as_posix()
    norm = posixpath.normpath(raw)
    if norm.startswith("../") or norm == ".." or norm.startswith("/"):
        raise PathOutsideWorkspaceError(f"Caminho fora do workspace: {path}")
    return norm.lower() if os.name == "nt" else norm


class FileLockManager:
    def __init__(self, repo: Repository, project_root: Path, ttl_seconds: int = 1800):
        self.repo = repo
        self.project_root = Path(project_root)
        self.ttl_seconds = ttl_seconds

    def file_resource(self, path: str) -> str:
        return "file:" + normalize_file(self.project_root, path)

    @staticmethod
    def mcp_resource(server: str, target: str) -> str:
        return f"mcp:{server}:{target.strip().strip('/')}"

    def _log(self, con, resource: str, owner: str, action: str, detail: str = "") -> None:
        con.execute("INSERT INTO lock_events(created_at, resource, owner, action, detail) VALUES(?,?,?,?,?)",
                    (utc_now(), resource, owner, action, detail[:500]))

    def release_expired(self) -> int:
        now = utc_now()
        with self.repo.connect() as con:
            rows = con.execute("SELECT resource, owner FROM file_locks WHERE expires_at <= ?", (now,)).fetchall()
            for r in rows:
                self._log(con, r["resource"], r["owner"], "expired", "liberado automaticamente por timeout")
            con.execute("DELETE FROM file_locks WHERE expires_at <= ?", (now,))
        return len(rows)

    def acquire(self, resources: list[str], owner: str, workflow_id: str | None = None, subtask_id: str | None = None,
                ttl_seconds: int | None = None) -> LockGrant:
        resources = sorted(set(resources))
        ttl = ttl_seconds or self.ttl_seconds
        now_dt = datetime.now(timezone.utc)
        now, expires = now_dt.isoformat(), (now_dt + timedelta(seconds=ttl)).isoformat()
        with self.repo.connect() as con:
            # Expirados são liberados na mesma transação.
            for r in con.execute("SELECT resource, owner FROM file_locks WHERE expires_at <= ?", (now,)).fetchall():
                self._log(con, r["resource"], r["owner"], "expired", "liberado automaticamente por timeout")
            con.execute("DELETE FROM file_locks WHERE expires_at <= ?", (now,))
            conflicts = []
            for res in resources:
                row = con.execute("SELECT * FROM file_locks WHERE resource=?", (res,)).fetchone()
                if row and not (row["owner"] == owner and row["subtask_id"] == subtask_id):
                    conflicts.append(dict(row))
            if not conflicts:
                for res in resources:
                    con.execute(
                        """INSERT INTO file_locks(resource, owner, workflow_id, subtask_id, acquired_at, expires_at) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(resource) DO UPDATE SET expires_at=excluded.expires_at""",
                        (res, owner, workflow_id, subtask_id, now, expires),
                    )
                    self._log(con, res, owner, "acquired", f"subtask {subtask_id}")
        if conflicts:
            # Registrado em transação própria (a de aquisição não grava nada: tudo ou nada).
            with self.repo.connect() as con:
                for c in conflicts:
                    self._log(con, c["resource"], owner, "conflict", f"pedido por {owner} ({subtask_id}); detentor {c['owner']} ({c['subtask_id']})")
            raise LockConflictError(conflicts)
        return LockGrant(resources, owner, expires)

    async def acquire_wait(self, resources: list[str], owner: str, workflow_id: str | None, subtask_id: str | None,
                           wait_seconds: float, poll: float = 1.0) -> LockGrant:
        deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
        while True:
            try:
                return self.acquire(resources, owner, workflow_id, subtask_id)
            except LockConflictError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(poll)

    def renew(self, resources: list[str], owner: str, ttl_seconds: int | None = None) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds or self.ttl_seconds)).isoformat()
        with self.repo.connect() as con:
            for res in resources:
                con.execute("UPDATE file_locks SET expires_at=? WHERE resource=? AND owner=?", (expires, res, owner))

    def release(self, resources: list[str], owner: str, subtask_id: str | None = None) -> int:
        count = 0
        with self.repo.connect() as con:
            for res in resources:
                cur = con.execute("DELETE FROM file_locks WHERE resource=? AND owner=? AND (subtask_id IS ? OR ? IS NULL)",
                                  (res, owner, subtask_id, subtask_id))
                if cur.rowcount:
                    count += 1
                    self._log(con, res, owner, "released", f"subtask {subtask_id}")
        return count

    def release_workflow(self, workflow_id: str) -> int:
        with self.repo.connect() as con:
            rows = con.execute("SELECT resource, owner FROM file_locks WHERE workflow_id=?", (workflow_id,)).fetchall()
            for r in rows:
                self._log(con, r["resource"], r["owner"], "released", f"fim do workflow {workflow_id}")
            con.execute("DELETE FROM file_locks WHERE workflow_id=?", (workflow_id,))
        return len(rows)

    def list_locks(self) -> list[dict]:
        with self.repo.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM file_locks ORDER BY acquired_at").fetchall()]

    def history(self, limit: int = 200) -> list[dict]:
        with self.repo.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM lock_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    @staticmethod
    def independent(files_a: list[str], files_b: list[str]) -> bool:
        return not (set(files_a) & set(files_b))
