"""Esquema SQLite e migrações.

Compatível com o banco da v0.2: as tabelas `tasks` e `task_events` são
preservadas e recebem colunas novas via ALTER TABLE (sem perda de dados).
As tarefas A2A do SDK ficam na tabela padrão `tasks` do SDK em orchestrator_a2a.db (DatabaseTaskStore oficial).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

BASE_TABLES = [
    # --- v0.2 (preservadas) ---
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        goal TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        result_json TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        agent TEXT NOT NULL,
        event TEXT NOT NULL,
        content TEXT NOT NULL
    )""",
    # --- v0.3 ---
    """CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS subtasks (
        id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        plan_id TEXT,
        title TEXT NOT NULL,
        agent TEXT NOT NULL,
        status TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        files_json TEXT NOT NULL DEFAULT '[]',
        depends_on_json TEXT NOT NULL DEFAULT '[]',
        review_cycles INTEGER NOT NULL DEFAULT 0,
        a2a_context_id TEXT,
        last_a2a_task_id TEXT,
        result_json TEXT,
        review_json TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        workflow_id TEXT,
        subtask_id TEXT,
        direction TEXT NOT NULL,
        sender TEXT NOT NULL,
        recipient TEXT NOT NULL,
        skill TEXT,
        a2a_message_id TEXT,
        a2a_task_id TEXT,
        a2a_context_id TEXT,
        a2a_state TEXT,
        summary TEXT,
        payload_json TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        agent TEXT NOT NULL,
        model TEXT,
        a2a_task_id TEXT,
        workflow_id TEXT,
        subtask_id TEXT,
        skill TEXT,
        session_id TEXT,
        status TEXT NOT NULL,
        duration_seconds REAL,
        usage_json TEXT,
        error TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS agent_sessions (
        agent TEXT NOT NULL,
        context_key TEXT NOT NULL,
        session_id TEXT NOT NULL,
        model TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (agent, context_key)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title TEXT NOT NULL,
        decision TEXT NOT NULL,
        rationale TEXT,
        author TEXT NOT NULL,
        status TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]',
        workflow_id TEXT,
        subtask_id TEXT,
        supersedes INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS file_locks (
        resource TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        workflow_id TEXT,
        subtask_id TEXT,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lock_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        resource TEXT NOT NULL,
        owner TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS file_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        workflow_id TEXT,
        subtask_id TEXT,
        agent TEXT NOT NULL,
        path TEXT NOT NULL,
        change_type TEXT NOT NULL,
        within_reservation INTEGER NOT NULL,
        source TEXT NOT NULL,
        git_head TEXT
    )""",
]

# Colunas adicionadas às tabelas da v0.2.
ADDED_COLUMNS = {
    "tasks": [
        ("phase", "TEXT"),
        ("mode", "TEXT DEFAULT 'full'"),
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("context_hash", "TEXT"),
        ("context_sources_json", "TEXT"),
        ("plan_json", "TEXT"),
        ("version", "TEXT"),
    ],
    "task_events": [
        ("level", "TEXT NOT NULL DEFAULT 'info'"),
        ("subtask_id", "TEXT"),
    ],
}

# Schema 4 (papéis simétricos). Somente ADD COLUMN: idempotente e sem perda de dados.
ADDED_COLUMNS_V4 = {
    "tasks": [
        ("planned_by", "TEXT"),
        ("planned_by_fallback", "INTEGER NOT NULL DEFAULT 0"),
        ("metrics_json", "TEXT"),
    ],
    "subtasks": [
        ("assigned_to", "TEXT"),
        ("reviewer", "TEXT"),
        ("assignment_reason", "TEXT"),
        ("assignment_json", "TEXT"),
        ("checks_json", "TEXT"),
        ("escalation_json", "TEXT"),
        ("human_decision_json", "TEXT"),
        ("check_fix_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("impl_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ],
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_task_events_task ON task_events(task_id)",
    "CREATE INDEX IF NOT EXISTS ix_subtasks_wf ON subtasks(workflow_id)",
    "CREATE INDEX IF NOT EXISTS ix_msgs_wf ON agent_messages(workflow_id)",
    "CREATE INDEX IF NOT EXISTS ix_runs_agent ON agent_runs(agent)",
    "CREATE INDEX IF NOT EXISTS ix_changes_wf ON file_changes(workflow_id)",
]


# Schema 5: estado da revisão final (nunca silencioso).
ADDED_COLUMNS_V5 = {
    "tasks": [
        ("final_review_state", "TEXT"),
        ("final_review_json", "TEXT"),
    ],
}


def read_schema_version(db_path: Path) -> int | None:
    """Versão do banco existente: None = banco novo/vazio; 2 = v0.2 (sem schema_meta); 3+ = schema_meta."""
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None
    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "tasks" not in tables:
            return None
        if "schema_meta" not in tables:
            return 2
        row = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else 2
    finally:
        con.close()


def backup_before_migration(db_path: Path) -> Path | None:
    """Copia o banco para <nome>.bak-v<versão> antes de subir o schema (não sobrescreve backup existente).

    Usa a API de backup do SQLite (consistente mesmo com WAL).
    """
    version = read_schema_version(db_path)
    if version is None or version >= SCHEMA_VERSION:
        return None
    bak = db_path.with_name(f"{db_path.name}.bak-v{version}")
    if bak.exists():
        return None
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(bak)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return bak


def _add_columns(con: sqlite3.Connection, spec: dict) -> None:
    for table, cols in spec.items():
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def migrate(con: sqlite3.Connection) -> None:
    for ddl in BASE_TABLES:
        con.execute(ddl)
    _add_columns(con, ADDED_COLUMNS)
    _add_columns(con, ADDED_COLUMNS_V4)
    _add_columns(con, ADDED_COLUMNS_V5)
    # Linhas antigas: quem implementou era `agent`. Revisor fica NULL (não inventamos histórico).
    con.execute("UPDATE subtasks SET assigned_to=agent WHERE assigned_to IS NULL")
    for ddl in INDEXES:
        con.execute(ddl)
    con.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
