"""Análise determinística de dependências entre subtarefas planejadas.

Sem LLM e sem executar o projeto. Valida a estrutura do plano, classifica pares de
subtarefas, constrói o DAG e produz grupos topológicos conservadores.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from orchestrator.file_locks import PathOutsideWorkspaceError, normalize_file


class DependencyClass(str, Enum):
    PARALLEL = "PARALLEL"
    SEQUENTIAL = "SEQUENTIAL"
    SHARED = "SHARED"


@dataclass(frozen=True)
class TaskFacts:
    task_id: str
    files: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()
    contracts: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    uncertain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relation:
    source: str
    target: str
    classification: DependencyClass
    reasons: tuple[str, ...]
    shared_files: tuple[str, ...] = ()
    shared_contracts: tuple[str, ...] = ()
    shared_resources: tuple[str, ...] = ()


@dataclass
class AnalysisResult:
    valid: bool
    policy_version: str
    classifications: dict[str, str] = field(default_factory=dict)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    groups: list[list[str]] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "policy_version": self.policy_version,
            "classifications": self.classifications,
            "dependencies": self.dependencies,
            "groups": self.groups,
            "relations": [
                {
                    "source": r.source,
                    "target": r.target,
                    "classification": r.classification.value,
                    "reasons": list(r.reasons),
                    "shared_files": list(r.shared_files),
                    "shared_contracts": list(r.shared_contracts),
                    "shared_resources": list(r.shared_resources),
                }
                for r in self.relations
            ],
            "errors": self.errors,
            "warnings": self.warnings,
        }


class DependencyAnalyzer:
    """Valida e agrupa subtarefas. Em dúvida, nunca libera paralelismo."""

    def __init__(self, workspace: Path, *, policy_version: str = "1.0", mcp_parallel_safe: bool = False,
                 policy_path: Path | None = None):
        self.workspace = Path(workspace)
        if policy_path:
            p = Path(policy_path)
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    policy_version = str(data.get("version") or policy_version)
                    mcp_parallel_safe = bool(data.get("mcp_parallel_safe", mcp_parallel_safe))
                except Exception:
                    pass
        self.policy_version = policy_version
        self.mcp_parallel_safe = mcp_parallel_safe

    def analyze(self, tasks: Iterable[dict[str, Any]], facts: dict[str, TaskFacts] | None = None) -> AnalysisResult:
        raw = [dict(t) for t in tasks]
        result = AnalysisResult(valid=False, policy_version=self.policy_version)
        ids = [str(t.get("id") or "").strip() for t in raw]
        missing = [i for i, task_id in enumerate(ids, 1) if not task_id]
        if missing:
            result.errors.append({"code": "missing_id", "tasks": missing})
        duplicates = sorted({task_id for task_id in ids if task_id and ids.count(task_id) > 1})
        if duplicates:
            result.errors.append({"code": "duplicate_ids", "ids": duplicates})
        if result.errors:
            return result

        by_id = {str(t["id"]): t for t in raw}
        normalized: dict[str, TaskFacts] = {}
        for task_id, task in by_id.items():
            try:
                files = frozenset(normalize_file(self.workspace, p) for p in task.get("files") or [])
                reads = frozenset(normalize_file(self.workspace, p) for p in task.get("reads") or [])
            except PathOutsideWorkspaceError as exc:
                result.errors.append({"code": "path_outside_workspace", "task_id": task_id, "detail": str(exc)})
                continue
            supplied = (facts or {}).get(task_id)
            resources = set(task.get("resources") or [])
            contracts = set(task.get("contracts") or [])
            uncertain = list(task.get("uncertain") or [])
            if supplied:
                files |= supplied.files
                reads |= supplied.reads
                resources |= set(supplied.resources)
                contracts |= set(supplied.contracts)
                uncertain += list(supplied.uncertain)
            normalized[task_id] = TaskFacts(task_id, files, reads, frozenset(contracts), frozenset(resources), tuple(uncertain))

        all_ids = set(by_id)
        dependencies: dict[str, set[str]] = {task_id: set() for task_id in by_id}
        for task_id, task in by_id.items():
            deps = [str(x) for x in task.get("depends_on") or []]
            nonexistent = sorted(set(deps) - all_ids)
            if nonexistent:
                result.errors.append({"code": "unknown_dependency", "task_id": task_id, "ids": nonexistent})
            if task_id in deps:
                result.errors.append({"code": "self_dependency", "task_id": task_id})
            dependencies[task_id].update(d for d in deps if d in all_ids and d != task_id)
        if result.errors:
            return result

        cycle = self._find_cycle(dependencies)
        if cycle:
            result.errors.append({"code": "dependency_cycle", "cycle": cycle})
            return result

        ids_order = list(by_id)
        task_class = {task_id: DependencyClass.PARALLEL for task_id in ids_order}
        relations: list[Relation] = []
        for i, left_id in enumerate(ids_order):
            for right_id in ids_order[i + 1 :]:
                left, right = normalized[left_id], normalized[right_id]
                relation = self._relation(left, right, dependencies)
                if relation:
                    relations.append(relation)
                    if relation.classification == DependencyClass.SHARED:
                        task_class[left_id] = task_class[right_id] = DependencyClass.SHARED
                        # Ordem conservadora conforme a ordem declarada do plano.
                        dependencies[right_id].add(left_id)
                    elif relation.classification == DependencyClass.SEQUENTIAL:
                        if task_class[left_id] == DependencyClass.PARALLEL:
                            task_class[left_id] = DependencyClass.SEQUENTIAL
                        if task_class[right_id] == DependencyClass.PARALLEL:
                            task_class[right_id] = DependencyClass.SEQUENTIAL
                        # Incerteza sem dependência declarada: serializa na ordem do plano.
                        if left_id not in dependencies[right_id] and right_id not in dependencies[left_id]:
                            dependencies[right_id].add(left_id)

        # Dependência explícita sempre torna o dependente sequencial.
        for task_id, deps in dependencies.items():
            if deps and task_class[task_id] == DependencyClass.PARALLEL:
                task_class[task_id] = DependencyClass.SEQUENTIAL

        cycle = self._find_cycle(dependencies)
        if cycle:
            result.errors.append({"code": "coordination_cycle", "cycle": cycle})
            return result

        result.valid = True
        result.relations = relations
        result.classifications = {k: v.value for k, v in task_class.items()}
        result.dependencies = {k: sorted(v) for k, v in dependencies.items()}
        result.groups = self._topological_groups(dependencies, ids_order)
        return result

    def _relation(self, a: TaskFacts, b: TaskFacts, deps: dict[str, set[str]]) -> Relation | None:
        if a.task_id in deps[b.task_id] or b.task_id in deps[a.task_id]:
            return Relation(a.task_id, b.task_id, DependencyClass.SEQUENTIAL, ("dependência declarada",))
        same_files = sorted(a.files & b.files)
        write_read = sorted((a.files & b.reads) | (b.files & a.reads))
        contracts = sorted(a.contracts & b.contracts)
        resources = sorted(a.resources & b.resources)
        reasons: list[str] = []
        if same_files:
            reasons.append("mesmo arquivo modificado")
        if write_read:
            reasons.append("uma tarefa modifica arquivo consumido pela outra")
        if contracts:
            reasons.append("contrato compartilhado")
        if resources:
            reasons.append("recurso compartilhado")
        if resources and not self.mcp_parallel_safe and any(r.startswith("mcp:") for r in resources):
            reasons.append("recurso MCP sem isolamento validado")
        if reasons:
            return Relation(a.task_id, b.task_id, DependencyClass.SHARED, tuple(reasons),
                            tuple(sorted(set(same_files + write_read))), tuple(contracts), tuple(resources))
        uncertain = tuple(dict.fromkeys(a.uncertain + b.uncertain))
        if uncertain:
            return Relation(a.task_id, b.task_id, DependencyClass.SEQUENTIAL,
                            tuple(f"incerteza: {x}" for x in uncertain))
        return None

    @staticmethod
    def _find_cycle(graph: dict[str, set[str]]) -> list[str]:
        state: dict[str, int] = {}
        stack: list[str] = []
        def visit(node: str) -> list[str]:
            state[node] = 1
            stack.append(node)
            for dep in graph[node]:
                if state.get(dep) == 1:
                    i = stack.index(dep)
                    return stack[i:] + [dep]
                if state.get(dep, 0) == 0:
                    found = visit(dep)
                    if found:
                        return found
            stack.pop()
            state[node] = 2
            return []
        for node in graph:
            if state.get(node, 0) == 0:
                found = visit(node)
                if found:
                    return found
        return []

    @staticmethod
    def _topological_groups(graph: dict[str, set[str]], order: list[str]) -> list[list[str]]:
        remaining = {node: set(deps) for node, deps in graph.items()}
        groups: list[list[str]] = []
        while remaining:
            ready = [node for node in order if node in remaining and not remaining[node]]
            if not ready:  # defesa; ciclos já foram validados
                return groups
            groups.append(ready)
            for node in ready:
                remaining.pop(node)
            for deps in remaining.values():
                deps.difference_update(ready)
        return groups
