"""Integração determinística da v0.5, sem A2A e sem modelos."""
import json
import sqlite3
from pathlib import Path

from core.config import load_settings
from database.models import SCHEMA_VERSION
from database.repository import Repository
from orchestrator.dependency_analyzer import DependencyAnalyzer
from orchestrator.schemas import Plan


def test_defaults_keep_parallelism_off(make_settings):
    s = make_settings()
    assert s.parallel_execution is False
    assert s.max_parallel_subtasks == 1
    assert s.parallel_per_agent == 1


def test_plan_contract_accepts_dependency_facts():
    p = Plan.model_validate({"tasks": [{"id": "T1", "title": "x", "files": ["a.lua"],
        "reads": ["b.lua"], "contracts": ["Profile"], "resources": ["mcp:roblox_studio:session"],
        "uncertain": ["require dinâmico"]}]})
    t = p.tasks[0]
    assert t.reads == ["b.lua"] and t.contracts == ["Profile"] and t.resources[0].startswith("mcp:")


def test_schema_6_migrates_and_persists_analysis(make_settings):
    s = make_settings()
    repo = Repository(s.db_path)
    repo.create_workflow("wf", "objetivo valido", "full", "0.5.0")
    analysis = DependencyAnalyzer(s.project_root).analyze([
        {"id": "T1", "title": "A", "files": ["a.lua"]},
        {"id": "T2", "title": "B", "files": ["b.lua"]},
    ]).to_dict()
    repo.save_dependency_analysis("wf", analysis, 1)
    wf = repo.get_workflow("wf")
    assert wf["dependency_analysis"]["valid"] is True and wf["plan_reformulations"] == 1
    with repo.connect() as con:
        version = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        cols = {r[1] for r in con.execute("PRAGMA table_info(subtasks)")}
    assert version == str(SCHEMA_VERSION) and int(version) >= 6
    assert {"dependency_class", "execution_group", "resources_json", "started_at", "finished_at"} <= cols


def test_apply_analysis_to_subtasks(make_settings):
    s = make_settings()
    repo = Repository(s.db_path)
    repo.create_workflow("wf", "objetivo valido", "full", "0.5.0")
    for seq, (pid, file) in enumerate((("T1", "a.lua"), ("T2", "b.lua")), 1):
        repo.create_subtask({"id": f"s{seq}", "workflow_id": "wf", "seq": seq, "plan_id": pid,
            "title": pid, "agent": "deepseek", "spec": {"id": pid, "files": [file]}, "files": [file], "depends_on": []})
    analysis = DependencyAnalyzer(s.project_root).analyze([
        {"id": "T1", "title": "A", "files": ["a.lua"]},
        {"id": "T2", "title": "B", "files": ["b.lua"]},
    ]).to_dict()
    repo.apply_dependency_analysis("wf", analysis)
    subs = repo.list_subtasks("wf")
    assert [x["execution_group"] for x in subs] == [1, 1]
    assert [x["dependency_class"] for x in subs] == ["PARALLEL", "PARALLEL"]


def test_stage_log_roundtrip(make_settings):
    repo = Repository(make_settings().db_path)
    repo.create_workflow("wf", "objetivo valido", "full", "0.5.0")
    sid = repo.start_stage("wf", "subtask", subtask_id="s1", agent="gemini", execution_group=2, detail={"x": 1})
    repo.finish_stage(sid, "finished", {"ok": True})
    rows = repo.list_stages("wf")
    assert rows[0]["status"] == "finished" and rows[0]["detail"] == {"ok": True} and rows[0]["finished_at"]


def test_policy_file_has_safe_defaults():
    data = json.loads((Path(__file__).parents[2] / "config" / "dependency_policy.json").read_text(encoding="utf-8"))
    assert data["parallel_execution"] is False and data["max_parallel_subtasks"] == 1
    assert data["parallel_per_agent"] == 1 and data["mcp_parallel_safe"] is False


def test_opencode_interrupt_route_is_declared():
    data = json.loads((Path(__file__).parents[2] / "config" / "opencode_routes.json").read_text(encoding="utf-8"))
    route = data["routes"]["session_abort"]
    assert route["method"] == "POST" and route["path"].endswith("/interrupt")


def test_invalid_plan_has_no_groups(tmp_path):
    result = DependencyAnalyzer(tmp_path).analyze([{"id": "T1", "title": "x", "depends_on": ["missing"]}])
    assert not result.valid and result.groups == [] and result.errors[0]["code"] == "unknown_dependency"
