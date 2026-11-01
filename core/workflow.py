"""Motor de Workflow Dinâmico e DAG de Etapas de Execução.

Implementa a separação rigorosa entre:
1. DAG de Tarefas (dependências de código e arquivos entre subtarefas).
2. DAG de Workflow (sequência e dependências entre etapas do ciclo de vida:
   contexto -> planejamento -> negociação -> subtarefas -> revisão final -> conclusão).

Permite fluxos dinâmicos, customizados, condicionais e saltos de etapas desnecessárias
(ex: tarefas de geração de assets simples sem revisão cruzada).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

log = logging.getLogger("core.workflow")


class WorkflowStageStatus(str, Enum):
    """Status de execução de uma etapa do DAG de workflow."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class WorkflowDAGError(RuntimeError):
    """Erro estrutural no DAG de workflow."""


class CycleDetectedError(WorkflowDAGError):
    """Ciclo detectado no DAG de etapas do workflow."""


class InvalidStageDependencyError(WorkflowDAGError):
    """Etapa referencia uma dependência inexistente."""


@dataclass
class WorkflowContext:
    """Contexto compartilhado e mutável durante a execução das etapas do DAG."""
    wf_id: str
    goal: str
    mode: str = "full"
    resume: bool = False
    bundle: Any | None = None
    mcp_status: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    paused: bool = False
    final_review: dict[str, Any] = field(default_factory=dict)
    stage_results: dict[str, Any] = field(default_factory=dict)
    stage_statuses: dict[str, WorkflowStageStatus] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowStage:
    """Definição de uma etapa no DAG de workflow."""
    id: str
    name: str
    action_name: str
    depends_on: tuple[str, ...] = ()
    description: str = ""
    can_skip: bool = False
    condition: Callable[[WorkflowContext], bool] | None = None


class WorkflowDAG:
    """Grafo acíclico dirigido (DAG) que define a sequência de etapas de um workflow."""

    def __init__(self, stages: list[WorkflowStage] | None = None) -> None:
        self.stages: dict[str, WorkflowStage] = {}
        if stages:
            for s in stages:
                self.add_stage(s)

    def add_stage(self, stage: WorkflowStage) -> None:
        """Adiciona uma etapa ao grafo."""
        self.stages[stage.id] = stage

    def remove_stage(self, stage_id: str) -> None:
        """Remove uma etapa do grafo."""
        self.stages.pop(stage_id, None)

    def get_stage(self, stage_id: str) -> WorkflowStage | None:
        """Recupera uma etapa pelo ID."""
        return self.stages.get(stage_id)

    def validate(self) -> None:
        """Valida que todas as dependências existem e não há ciclos."""
        for sid, stage in self.stages.items():
            for dep in stage.depends_on:
                if dep not in self.stages:
                    raise InvalidStageDependencyError(
                        f"Etapa '{sid}' depende da etapa inexistente '{dep}'."
                    )

        # Detecção de ciclos via ordenação topológica (Algoritmo de Kahn)
        in_degree = {sid: 0 for sid in self.stages}
        for stage in self.stages.values():
            for dep in stage.depends_on:
                in_degree[stage.id] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited_count = 0

        while queue:
            curr = queue.pop(0)
            visited_count += 1
            for stage in self.stages.values():
                if curr in stage.depends_on:
                    in_degree[stage.id] -= 1
                    if in_degree[stage.id] == 0:
                        queue.append(stage.id)

        if visited_count != len(self.stages):
            raise CycleDetectedError("Ciclo detectado nas dependências das etapas do workflow.")

    def topological_sort(self) -> list[WorkflowStage]:
        """Retorna as etapas na ordem topológica de execução."""
        self.validate()
        in_degree = {sid: len(s.depends_on) for sid, s in self.stages.items()}
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        order: list[WorkflowStage] = []

        while queue:
            curr = queue.pop(0)
            order.append(self.stages[curr])
            for stage in self.stages.values():
                if curr in stage.depends_on:
                    in_degree[stage.id] -= 1
                    if in_degree[stage.id] == 0:
                        queue.append(stage.id)

        return order

    def execution_waves(self) -> list[list[WorkflowStage]]:
        """Agrupa etapas que podem ser executadas na mesma onda de paralelismo."""
        self.validate()
        in_degree = {sid: len(s.depends_on) for sid, s in self.stages.items()}
        current_wave = [sid for sid, deg in in_degree.items() if deg == 0]
        waves: list[list[WorkflowStage]] = []

        while current_wave:
            waves.append([self.stages[sid] for sid in current_wave])
            next_wave = []
            for curr in current_wave:
                for stage in self.stages.values():
                    if curr in stage.depends_on:
                        in_degree[stage.id] -= 1
                        if in_degree[stage.id] == 0:
                            next_wave.append(stage.id)
            current_wave = next_wave

        return waves

    # ---------------------------------------------------------------- fábricas padrão
    @classmethod
    def standard_dag(cls) -> WorkflowDAG:
        """DAG padrão completo: Contexto -> Planejamento -> Negociação -> Subtarefas -> Revisão Final -> Conclusão."""
        return cls([
            WorkflowStage(id="context", name="Coleta de Contexto", action_name="context", depends_on=()),
            WorkflowStage(id="plan", name="Planejamento", action_name="plan", depends_on=("context",)),
            WorkflowStage(id="negotiate", name="Atribuição de Papéis", action_name="negotiate", depends_on=("plan",)),
            WorkflowStage(id="execute_subtasks", name="Execução de Subtarefas", action_name="execute_subtasks", depends_on=("negotiate",)),
            WorkflowStage(id="final_review", name="Revisão Final", action_name="final_review", depends_on=("execute_subtasks",)),
            WorkflowStage(id="conclude", name="Conclusão e Métricas", action_name="conclude", depends_on=("final_review",)),
        ])

    @classmethod
    def simple_dag(cls) -> WorkflowDAG:
        """DAG simplificado (fast-track): Contexto -> Planejamento -> Negociação (direta/sem LLM) -> Subtarefas -> Revisão Final -> Conclusão."""
        return cls([
            WorkflowStage(id="context", name="Coleta de Contexto", action_name="context", depends_on=()),
            WorkflowStage(id="plan", name="Planejamento", action_name="plan", depends_on=("context",)),
            WorkflowStage(id="negotiate", name="Atribuição Direta", action_name="negotiate", depends_on=("plan",)),
            WorkflowStage(id="execute_subtasks", name="Execução de Subtarefas", action_name="execute_subtasks", depends_on=("negotiate",)),
            WorkflowStage(id="final_review", name="Revisão Final (Condicional)", action_name="final_review", depends_on=("execute_subtasks",)),
            WorkflowStage(id="conclude", name="Conclusão e Métricas", action_name="conclude", depends_on=("final_review",)),
        ])

    @classmethod
    def plan_only_dag(cls) -> WorkflowDAG:
        """DAG restrito a planejamento: Contexto -> Planejamento -> Conclusão."""
        return cls([
            WorkflowStage(id="context", name="Coleta de Contexto", action_name="context", depends_on=()),
            WorkflowStage(id="plan", name="Planejamento", action_name="plan", depends_on=("context",)),
            WorkflowStage(id="conclude", name="Conclusão", action_name="conclude", depends_on=("plan",)),
        ])

    @classmethod
    def no_review_dag(cls) -> WorkflowDAG:
        """DAG sem revisão (rápido): Contexto -> Planejamento -> Negociação -> Subtarefas -> Conclusão."""
        return cls([
            WorkflowStage(id="context", name="Coleta de Contexto", action_name="context", depends_on=()),
            WorkflowStage(id="plan", name="Planejamento", action_name="plan", depends_on=("context",)),
            WorkflowStage(id="negotiate", name="Atribuição de Papéis", action_name="negotiate", depends_on=("plan",)),
            WorkflowStage(id="execute_subtasks", name="Execução de Subtarefas", action_name="execute_subtasks", depends_on=("negotiate",)),
            WorkflowStage(id="conclude", name="Conclusão e Métricas", action_name="conclude", depends_on=("execute_subtasks",)),
        ])

    @classmethod
    def resume_final_review_dag(cls) -> WorkflowDAG:
        """DAG de retomada exclusivamente para revisão final."""
        return cls([
            WorkflowStage(id="final_review", name="Revisão Final", action_name="final_review", depends_on=()),
            WorkflowStage(id="conclude", name="Conclusão e Métricas", action_name="conclude", depends_on=("final_review",)),
        ])


class WorkflowEngine:
    """Motor de execução do DAG de workflow."""

    async def execute(
        self,
        dag: WorkflowDAG,
        context: WorkflowContext,
        handlers: dict[str, Callable[[WorkflowContext], Coroutine[Any, Any, Any]]],
    ) -> str:
        """Executa as etapas do DAG na ordem topológica respeitando condições e dependências."""
        stages = dag.topological_sort()
        for stage in stages:
            # Se alguma dependência falhou, pula esta etapa
            failed_deps = [dep for dep in stage.depends_on if context.stage_statuses.get(dep) == WorkflowStageStatus.FAILED]
            if failed_deps:
                context.stage_statuses[stage.id] = WorkflowStageStatus.SKIPPED
                continue

            # Avalia condição personalizada da etapa
            if stage.condition and not stage.condition(context):
                context.stage_statuses[stage.id] = WorkflowStageStatus.SKIPPED
                continue

            handler = handlers.get(stage.action_name or stage.id)
            if not handler:
                raise RuntimeError(f"Nenhum handler registrado para a etapa '{stage.id}' (ação: '{stage.action_name}').")

            context.stage_statuses[stage.id] = WorkflowStageStatus.RUNNING
            try:
                res = await handler(context)
                context.stage_results[stage.id] = res
                context.stage_statuses[stage.id] = WorkflowStageStatus.COMPLETED
                if stage.id == "conclude" and isinstance(res, str):
                    return res
            except Exception:
                context.stage_statuses[stage.id] = WorkflowStageStatus.FAILED
                raise

        return str(context.stage_results.get("conclude", "completed"))
