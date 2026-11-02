# Architecture Evolution Plan: Multi-Provider Workspace

[Português (Brasil)](ARCHITECTURE.pt-BR.md)

## Product vision
The long-term goal is to turn the orchestrator (currently focused on Roblox) into a **centralized, multi-agent, multi-provider AI workspace**. The system receives a request, identifies the domain (Roblox, web development, data science, etc.), selects the most suitable models/providers (OpenAI, Anthropic, Google, DeepSeek) and manages execution, validation and delivery in a single environment, removing the fragmentation of working with AI.

## Non-negotiable architectural principles
1. **Separate DAGs.** The task DAG (dependencies between subtasks) is never mixed with the workflow DAG (the sequence of execution stages).
2. **Independent core.** The core is blind to the domain. Its responsibility is safety, lock management (strict retention), budgets, concurrency, idempotency and failure recovery.
3. **Narrow provider abstraction.** `Provider`, `Model`, `Agent` and `Tool` are separate entities. The core does not assume every API has the same capabilities (preparing for a future *Capability Registry*).
4. **Passive domain.** Domain-specific rules (Luau, React, etc.) live exclusively in a `DomainProfile`, which injects instructions, validators and configuration into the core.

---

## Phase 0: Stabilization (✅ DONE)
- Centralized lock release (mandatory retention when the external process cannot be confirmed as terminated: `expired_retained`).
- Strict `stage_log` lifecycle, recording the exact database status and preventing failures from being masked.
- Exclusive injection of the dependency policy (`config/dependency_policy.json`).
- Encoding fixes and consolidated tests for the topological cycle and structural failures.

---

## Phase 1: Passive domain extraction (✅ DONE)
- Abstract interface `core/domain.py` (`DomainProfile`).
- Roblox rules and prompts isolated in `domains/roblox/`.
- Full decoupling: zero domain terms in `orchestrator/` and `communication/`.
- `domain` injected into `Container`, `Coordinator` and `RoleAgentExecutor`.
- 163 unit and integration tests passing.

---

## Phase 2: Provider abstraction (✅ DONE)
- `CapabilityRegistry`, `ModelCapability` and `ModelSpec` in `core/capabilities.py`, supporting heterogeneous capabilities (reasoning, vision, code, tools, etc.).
- Abstract `Provider` base interface in `core/providers.py` (exclusively responsible for network/SDK/CLI, quotas, cooldowns and health checks).
- `GeminiProvider`, `OpenCodeProvider` and `MockProvider` implemented.
- `Agent` entity in `core/agent.py`, combining persona, skills and a binding to a model and provider.
- Required-capability checks in `AgentRegistry.select()`.
- Full backward compatibility with `AgentBackend`.
- 169 tests passing (0 regressions).

---

## Phase 3: Dynamic workflow engine (✅ DONE)
- `WorkflowDAG`, `WorkflowStage`, `WorkflowContext` and `WorkflowEngine` in `core/workflow.py`.
- Strict separation between the task DAG (subtasks and files) and the workflow DAG (lifecycle stages).
- Structural DAG validation (cycle and missing-dependency detection) and computation of concurrent execution waves.
- Coordinator refactored to run stages through a dynamic DAG (`standard_dag`, `plan_only_dag`, `no_review_dag`, `resume_final_review_dag`).
- Dynamic cross-review skipping for simple tasks/assets (`skip_review` / `category='asset'`), saving tokens and time.
- 176 tests passing (0 regressions).

---

## Phase 4: Intelligent routing and unified workspace (✅ DONE)
- **Local vector store and RAG:** `VectorStore` in `memory/vector_store.py` with SQLite persistence and a deterministic dense vectorizer (`HashedDenseEmbedding`), enabling fast, offline semantic search with no external dependencies.
- **Semantic context manager:** automatic indexing of workspace documents and technical decisions in `ContextManager`, with `search_relevant` and `build_augmented` for precise context injection into planning and execution.
- **Capability- and cost-aware routing:** `IntelligentRouter` in `orchestrator/assignment.py` evaluates the models registered in the `CapabilityRegistry`, sending complex/architectural work to reasoning models and assets/docs/boilerplate to more cost-efficient ones.
- **Cost metrics and reports:** token and estimated-cost aggregation per model and agent (`Repository.get_cost_summary`), exposed through `GET /metrics/costs` and `GET /providers`.
- **Unified workspace and dashboard:** the dashboard (`dashboard/index.html`) supports multiple providers, capability badges (Reasoning, Code, Vision), cost tiers, a stage DAG view, an interactive RAG console and live cost reports.
- 183 tests passing (0 regressions).

---

## Next steps (continuous evolution)
- More domain profiles (full-stack web, data science, Python backend).
- Vision models and procedural generation of multimodal assets.
- Distributed monitoring and remote execution of agents in the cloud.
