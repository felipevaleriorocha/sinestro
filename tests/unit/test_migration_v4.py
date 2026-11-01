"""[MOCK] Migração para o schema 4: backup prévio, idempotência e preservação de dados v0.2/v0.3."""
import json
import sqlite3

from database.models import SCHEMA_VERSION, migrate
from database.repository import Repository


def _make_v3_db(path):
    """Banco no formato da v0.3 (schema 3) com dados."""
    import database.models as m
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for ddl in m.BASE_TABLES:
        con.execute(ddl)
    m._add_columns(con, m.ADDED_COLUMNS)
    con.execute("INSERT INTO schema_meta VALUES('schema_version','3')")
    con.execute("INSERT INTO tasks(id, goal, status, created_at, updated_at, result_json) VALUES('wf3','meta v0.3','completed','t','t',?)",
                (json.dumps({"ok": 1}),))
    con.execute("""INSERT INTO subtasks(id, workflow_id, seq, plan_id, title, agent, status, spec_json, created_at, updated_at, review_cycles)
                   VALUES('st3','wf3',1,'T1','antiga','deepseek','approved','{}','t','t',1)""")
    con.commit()
    con.close()


def test_backup_created_once_before_schema4(make_settings):
    s = make_settings()
    _make_v3_db(s.db_path)
    repo = Repository(s.db_path)
    bak = s.db_path.with_name("orchestrator.db.bak-v3")
    assert repo.backup_path == bak and bak.exists()
    with sqlite3.connect(bak) as con:  # backup ainda é v3 e tem os dados
        assert con.execute("SELECT value FROM schema_meta").fetchone()[0] == "3"
        assert con.execute("SELECT goal FROM tasks WHERE id='wf3'").fetchone()[0] == "meta v0.3"
        cols = {r[1] for r in con.execute("PRAGMA table_info(subtasks)")}
        assert "assigned_to" not in cols
    before = bak.read_bytes()
    repo2 = Repository(s.db_path)  # segunda abertura: já está no v4, não recria/sobrescreve
    assert repo2.backup_path is None and bak.read_bytes() == before


def test_migration_is_idempotent_and_keeps_data(make_settings):
    s = make_settings()
    _make_v3_db(s.db_path)
    Repository(s.db_path)
    with sqlite3.connect(s.db_path) as con:
        migrate(con)  # roda de novo direto
        migrate(con)
        assert con.execute("SELECT value FROM schema_meta").fetchone()[0] == str(SCHEMA_VERSION)
        row = con.execute("SELECT agent, assigned_to, reviewer, review_cycles, check_fix_attempts, impl_attempts FROM subtasks").fetchone()
        assert row == ("deepseek", "deepseek", None, 1, 0, 0)
    repo = Repository(s.db_path)
    assert repo.get_workflow("wf3")["result"] == {"ok": 1}


def test_v02_db_gets_backup_v2_and_migrates(make_settings):
    s = make_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(s.db_path) as con:
        con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error TEXT)")
        con.execute("INSERT INTO tasks VALUES('old','v0.2','completed','t','t',NULL,NULL)")
    repo = Repository(s.db_path)
    assert repo.backup_path.name == "orchestrator.db.bak-v2"
    assert repo.get_workflow("old")["goal"] == "v0.2"


def test_new_db_has_no_backup(make_settings):
    s = make_settings()
    assert Repository(s.db_path).backup_path is None
