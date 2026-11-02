from orchestrator.dependency_analyzer import DependencyAnalyzer, TaskFacts


def t(i, files=None, deps=None, **kw):
    return {"id": i, "title": i, "files": files or [], "depends_on": deps or [], **kw}


def analyze(tmp_path, tasks, facts=None, **kw):
    return DependencyAnalyzer(tmp_path, **kw).analyze(tasks, facts)


def test_two_independent_files_same_parallel_group(tmp_path):
    r = analyze(tmp_path, [t("A", ["a.lua"]), t("B", ["b.lua"])])
    assert r.valid and r.groups == [["A", "B"]]
    assert set(r.classifications.values()) == {"PARALLEL"}


def test_same_file_is_shared_and_sequentialized(tmp_path):
    r = analyze(tmp_path, [t("A", ["a.lua"]), t("B", ["a.lua"])])
    assert r.valid and r.groups == [["A"], ["B"]]
    assert r.classifications == {"A": "SHARED", "B": "SHARED"}


def test_declared_dependency(tmp_path):
    r = analyze(tmp_path, [t("A"), t("B", deps=["A"])])
    assert r.valid and r.groups == [["A"], ["B"]]
    assert r.classifications["B"] == "SEQUENTIAL"


def test_cycle_invalid(tmp_path):
    r = analyze(tmp_path, [t("A", deps=["B"]), t("B", deps=["A"])])
    assert not r.valid and r.errors[0]["code"] == "dependency_cycle"


def test_unknown_dependency_invalid(tmp_path):
    r = analyze(tmp_path, [t("A", deps=["X"])])
    assert not r.valid and r.errors[0]["code"] == "unknown_dependency"


def test_duplicate_ids_invalid(tmp_path):
    r = analyze(tmp_path, [t("A"), t("A")])
    assert not r.valid and r.errors[0]["code"] == "duplicate_ids"


def test_shared_contract(tmp_path):
    r = analyze(tmp_path, [t("A", contracts=["ProfileSchema"]), t("B", contracts=["ProfileSchema"])])
    assert r.valid and r.groups == [["A"], ["B"]]
    assert r.relations[0].shared_contracts == ("ProfileSchema",)


def test_module_modified_and_read_by_other(tmp_path):
    facts = {
        "A": TaskFacts("A", files=frozenset({"src/shared/M.lua"})),
        "B": TaskFacts("B", reads=frozenset({"src/shared/M.lua"})),
    }
    r = analyze(tmp_path, [t("A"), t("B")], facts)
    assert r.valid and r.classifications["A"] == "SHARED"


def test_topological_waves(tmp_path):
    r = analyze(tmp_path, [t("A"), t("B"), t("C", deps=["B"])])
    assert r.valid and r.groups == [["A", "B"], ["C"]]


def test_uncertainty_forces_sequential(tmp_path):
    facts = {"A": TaskFacts("A", uncertain=("require dinâmico",))}
    r = analyze(tmp_path, [t("A"), t("B")], facts)
    assert r.valid and r.groups == [["A"], ["B"]]


def test_mcp_shared_resource_is_shared(tmp_path):
    tasks = [t("A", resources=["mcp:roblox_studio:session"]), t("B", resources=["mcp:roblox_studio:session"])]
    r = analyze(tmp_path, tasks)
    assert r.valid and r.groups == [["A"], ["B"]]
    assert "recurso MCP sem isolamento validado" in r.relations[0].reasons


def test_distinct_mcp_targets_can_group_but_lock_layer_still_decides(tmp_path):
    tasks = [t("A", resources=["mcp:roblox_studio:Map"]), t("B", resources=["mcp:roblox_studio:UI"])]
    r = analyze(tmp_path, tasks)
    assert r.valid and r.groups == [["A", "B"]]


def test_path_outside_workspace_invalid(tmp_path):
    r = analyze(tmp_path, [t("A", ["../outside.lua"])])
    assert not r.valid and r.errors[0]["code"] == "path_outside_workspace"


def test_predecessor_failure_is_scheduler_concern_but_graph_preserves_edge(tmp_path):
    r = analyze(tmp_path, [t("A"), t("B", deps=["A"])])
    assert r.dependencies["B"] == ["A"]


def test_infrastructure_failure_is_not_a_code_finding(tmp_path):
    r = analyze(tmp_path, [t("A")])
    payload = r.to_dict()
    assert payload["errors"] == [] and "findings" not in payload


def test_concurrency_one_does_not_change_analysis_groups(tmp_path):
    r = analyze(tmp_path, [t("A"), t("B")])
    assert r.groups == [["A", "B"]]  # executor aplica o limite; Analyzer descreve independência


def test_shared_with_declared_order_preserves_safe_order(tmp_path):
    tasks = [t("A", ["a.lua"], deps=["B"]), t("B", ["a.lua"])]
    r = analyze(tmp_path, tasks)
    assert r.valid and r.groups == [["B"], ["A"]]
