"""[MOCK] Contratos de atribuição, tabela de desempate e proibição de autorrevisão."""
import json

import pytest
from pydantic import ValidationError

from database.repository import Repository
from orchestrator.assignment import AssignmentRules, other, resolve_assignment
from orchestrator.schemas import AssignmentDecision, AssignmentResponseItem, PlannedTask, ReviewResult

RULES = AssignmentRules(default_agent="gemini",
                        categories={"security": "gemini", "ui": "deepseek", "server": "gemini"},
                        path_rules=[{"pattern": "**/StarterGui/**", "agent": "deepseek", "label": "ui"},
                                    {"pattern": "src/server/**", "agent": "gemini", "label": "server"}])


def task(**kw):
    return PlannedTask.model_validate({"id": "T1", "title": "t", **kw}).model_dump()


def test_other_and_reviewer_never_equals_assignee():
    assert other("gemini") == "deepseek" and other("deepseek") == "gemini"
    with pytest.raises(ValidationError):
        AssignmentDecision(task_id="T1", assigned_to="gemini", reviewer="gemini", decided_by="consensus")


def test_plan_reviewer_field_is_ignored_and_agent_alias_kept():
    t = PlannedTask.model_validate({"title": "x", "agent": "gemini", "reviewer": "gemini"})
    assert t.assigned_to == "gemini" and t.agent == "gemini" and "reviewer" not in t.model_dump()


def test_repository_refuses_self_review(make_settings):
    s = make_settings()
    repo = Repository(s.db_path)
    with pytest.raises(ValueError):
        repo.update_subtask("x", assigned_to="deepseek", reviewer="deepseek")


def test_accept_keeps_proposal():
    d = resolve_assignment(task(assigned_to="deepseek", category="ui"), "gemini",
                           AssignmentResponseItem(task_id="T1", decision="accept"), "deepseek", RULES, None)
    assert (d.assigned_to, d.reviewer, d.decided_by) == ("deepseek", "gemini", "consensus")


def test_counter_resolved_by_category_rule():
    d = resolve_assignment(task(assigned_to="deepseek", category="security"), "gemini",
                           AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="gemini", reason="é segurança"),
                           "deepseek", RULES, None)
    assert (d.assigned_to, d.reviewer, d.decided_by, d.rule) == ("gemini", "deepseek", "rules", "category:security")
    d2 = resolve_assignment(task(assigned_to="gemini", category="ui"), "gemini",
                            AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="deepseek"), "deepseek", RULES, None)
    assert d2.assigned_to == "deepseek" and d2.reviewer == "gemini"


def test_counter_resolved_by_path_then_default():
    d = resolve_assignment(task(assigned_to="gemini", category="other", files=["src/client/StarterGui/Hud.lua"]), "gemini",
                           AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="deepseek"), "deepseek", RULES, None)
    assert d.assigned_to == "deepseek" and d.rule.startswith("path:")
    d2 = resolve_assignment(task(assigned_to="deepseek", category="other", files=["x.lua"]), "gemini",
                            AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="gemini"), "deepseek", RULES, None)
    assert d2.assigned_to == "gemini" and d2.rule == "default_agent"


def test_default_agent_configurable(tmp_path):
    f = tmp_path / "rules.json"
    f.write_text(json.dumps({"default_agent": "deepseek", "categories": {}, "path_rules": []}))
    rules = AssignmentRules.load(f)
    d = resolve_assignment(task(assigned_to="gemini", category="other"), "gemini",
                           AssignmentResponseItem(task_id="T1", decision="counter", proposed_agent="deepseek"), "deepseek", rules, None)
    assert d.assigned_to == "deepseek" and d.rule == "default_agent"


def test_gemini_without_quota_falls_back_to_deepseek():
    d = resolve_assignment(task(assigned_to="gemini", category="security"), "gemini", None, None, RULES, "quota excedida")
    assert (d.assigned_to, d.reviewer, d.decided_by) == ("deepseek", "gemini", "quota_fallback")
    assert "quota" in d.reason


def test_review_findings_accept_plain_strings_as_unverified():
    r = ReviewResult.model_validate({"verdict": "changes_requested", "findings": ["texto solto", {"file": "a.lua", "line": "3", "snippet": "x"}]})
    assert r.findings[0].file == "" and r.findings[0].issue == "texto solto"
    assert r.findings[1].line == 3
