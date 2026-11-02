# SINESTRO

[![Status](https://img.shields.io/badge/status-in%20development-orange)](#roadmap) [![Version](https://img.shields.io/badge/version-0.5.1-blue)](#) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** | [Português (Brasil)](README.pt-BR.md)

> **A unified workspace for AI collaboration.**

SINESTRO is a **local, autonomous multi-agent orchestrator** for coordinating software engineering workflows with AI agents that plan, implement, and **review one another's work**. Agents communicate through the **A2A 1.0** protocol using structured data contracts and deterministic validation rather than relying on unstructured chat.

> **Core rule:** an agent that implements a task must never review its own work. Cross-review is assigned to a different, independent agent.

## Current implementation

| Agent | Execution channel | Main configuration |
|---|---|---|
| **Gemini** | Antigravity CLI (`agy`), through a controlled subprocess | `AGY_MODEL` |
| **DeepSeek** | Local HTTP API / OpenCode Engine | `OPENCODE_MODEL` |

Models and providers are configured declaratively through environment variables (`.env`). The orchestration core is not intended to be permanently tied to a specific model.

**Current version:** `0.5.1` · **SQLite schema:** `7`

The current implementation includes static dependency analysis through the **Dependency Analyzer**, deterministic execution that is sequential by default (`PARALLEL_EXECUTION=false`), stage tracking through `stage_log`, and resource/tool isolation support through the **Model Context Protocol (MCP)**.

> **Development status:** SINESTRO is under active development. Features, configuration, and APIs may change. The multi-provider workspace described in the roadmap is the project's direction; not every capability is implemented yet.

## Task execution workflow

```text
1. Planning               The planning agent breaks the goal into structured subtasks with verifiable acceptance criteria.
2. Structural validation The Dependency Analyzer validates the task DAG without an LLM; one plan-reformulation attempt is allowed if validation fails.
3. Assignment             Round 1: an agent proposes an assignment. Round 2: the other agent accepts or raises a technical objection.
                          Deterministic tie-breaking uses config/assignment_rules.json.
4. Implementation         The assigned agent works strictly within reserved files and resources.
5. Automated checks       Linters, static analyzers, and local tests run before LLM-based reviews.
6. Cross-review           The OTHER agent reviews in read-only mode, providing strict evidence: file, line, and code excerpt.
7. Wave integration       Automated checks validate the combined files produced by a topological group.
8. Final review           Global integration review checks interfaces, contracts, consistency, and shared state.
```

Conflict-resolution hierarchy: **automated checks > reviewer with verifiable evidence > human escalation (`needs_human`)**.

## Final review states

The `final_review_state` field is persisted explicitly. The pure function `final_status()` in `orchestrator/task_manager.py` is the single authority for workflow status transitions:

| `final_review_state` | Workflow status (when subtasks are approved) |
|---|---|
| `passed` | `completed` |
| `disabled` (`FINAL_REVIEW=false`) | `completed` (with final review marked as disabled) |
| `accepted_by_human` | `completed` |
| `skipped` / `failed` | `completed_unreviewed` (**Resume** runs the pending final review only) |
| `changes_requested` / `rejected` / `unverified` / `invalidated` | `needs_human` |
| `rejected_by_human` | `failed` |

If all subtasks were implemented by one agent, the other agent takes responsibility for the entire final review. When authorship is mixed, the reviewer's approval applies only to tasks implemented by the other agent. Tasks authored by the reviewer are explicitly recorded in `self_authored_subtasks` for auditable coverage.

## Requirements

- Python 3.11 or later (`py -3 --version` or `python3 --version`).
- Antigravity CLI (`agy`), configured and authenticated.
- OpenCode Engine or a compatible local inference server.
- Git available on `PATH` (used strictly in **read-only mode** for snapshots and diffs).
- Optional: local static-analysis tools and linters (configured in `config/checks.json`).
- Optional: Node.js (only for the dashboard's static-rendering test suite).

## Installation and startup

```bash
git clone <repository-url> sinestro
cd sinestro
cp .env.example .env  # Set PROJECT_ROOT and the required credentials
```

On Windows (PowerShell):

```powershell
# Terminal 1: OpenCode support backend (when applicable)
.\run_opencode.ps1

# Terminal 2: Orchestrator (creates .venv and installs dependencies on first run)
.\start.ps1
```

Once the service is running:

- **Operations dashboard:** <http://127.0.0.1:8787/dashboard/>
- **API documentation (OpenAPI/Swagger):** <http://127.0.0.1:8787/docs>

## Environment configuration (`.env`)

The `.env` file contains local configuration and secrets. **Never commit it to version control.**

| Variable | Default | Description |
|---|---|---|
| `PROJECT_ROOT` | — | Absolute path to the managed workspace. |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | Local development server endpoint. |
| `OPENCODE_MODEL` | `opencode/deepseek-v4.1-flash-free` | Model identifier (`provider/model`). |
| `OPENCODE_AGENT` | `Build` | Implementation agent persona. |
| `OPENCODE_REVIEW_AGENT` | `Plan` | Read-only persona for planning and analysis. |
| `AGY_BIN` | `agy` | CLI executable or wrapper. |
| `AGY_MODEL` | `gemini-4-argon-high` | Main reasoning/architecture model. |
| `AGY_REVIEW_MODE` | `plan` | `--mode` flag used for strictly analytical tasks. |
| `PARALLEL_EXECUTION` | `false` | Enables concurrency between independent subtasks (safe default: disabled). |
| `MAX_PARALLEL_SUBTASKS` | `1` | Maximum number of concurrent subtasks. |
| `MAX_REVIEW_CYCLES` | `2` | Maximum number of correction cycles requested by a reviewer. |
| `MAX_CHECK_FIX_ATTEMPTS` | `2` | Automatic correction attempts after linter/check failures. |
| `MAX_IMPLEMENTATION_ATTEMPTS` | `4` | Global attempt limit per subtask before escalation. |
| `MAX_BLOCKED_SUBTASKS` | `3` | Defensive workflow pause threshold for blocked subtasks. |
| `FINAL_REVIEW` | `true` | Enables the global integration and final-validation stage. |
| `ORCH_MOCK_AGENTS` | `false` | Set to `true` to use in-memory mock agents for workflow tests. |

## Dependency Analyzer: deterministic task DAG

Before code is executed or delegated, the task plan is analyzed by `DependencyAnalyzer` (`orchestrator/dependency_analyzer.py`). It does not invoke language models or execute processes.

1. **Structural validation:** detects and blocks empty IDs, duplicate IDs, missing references in `depends_on`, dependency cycles, and paths outside the workspace.
2. **Static relationship classification:**
   - `PARALLEL`: task graphs are independent, with no mutual dependency or shared files/resources.
   - `SEQUENTIAL`: an explicit dependency, declared technical uncertainty, or dynamic loading requires ordered execution.
   - `SHARED`: tasks share files, contracts, persistence schemas, or MCP resources.
3. **Topological wave grouping:** orders execution into groups. Even in sequential mode, groups determine resource-reservation order and integration-check boundaries.

## REST API

| Action | Method / endpoint | Payload / description |
|---|---|---|
| Start workflow | `POST /tasks` | `{"goal": "...", "mode": "plan_only" \| "full"}` |
| Get workflow | `GET /tasks/{id}` | Returns status, DAG, events, metrics, and stages. |
| Reanalyze plan | `POST /tasks/{id}/analyze` | Recalculates relationships and topological groups without an LLM. |
| Get operational report | `GET /tasks/{id}/report` | Aggregates stages, changed files, and metrics. |
| Resume workflow | `POST /tasks/{id}/resume` | Resumes blocked, paused, or review-pending workflows. |
| Decide on subtask | `POST /tasks/{id}/subtasks/{sid}/decision` | Human action: `approve`, `retry`, `reassign`, or `reject`. |
| Decide on final review | `POST /tasks/{id}/final-review/decision` | Human action: `accept` or `reject`. |
| Cancel workflow | `POST /tasks/{id}/cancel` | Requests safe cancellation and records the stop audit. |
| Clear agent quota | `POST /agents/{name}/clear-quota` | Resets a persisted cooldown after quota availability is restored. |
| Integration status | `GET /checks/status` | Lists installed and active validation tools. |
| Lock manager | `GET /locks`, `GET /locks/history` | Shows currently reserved resources and the audit history. |

## Tests and validation

Run the complete unit and integration test suite with mocks:

```powershell
.\scripts\run_tests.ps1
```

Run local infrastructure integration tests without consuming model tokens:

```powershell
.\scripts\run_tests.ps1 -Live
```

Run an end-to-end operational test with real models (uses quota; run in an isolated workspace):

```powershell
.\scripts\manual_role_test.ps1 -Implementer deepseek
.\scripts\manual_role_test.ps1 -Implementer gemini
```

## Security and concurrency

- **Network binding:** the service exposes endpoints exclusively on `127.0.0.1`.
- **Data sanitization:** secrets, passwords, and authentication headers are redacted before being written to logs or the database.
- **Workspace boundary:** absolute or relative paths outside `PROJECT_ROOT` are rejected by validation layers.
- **Controlled subprocesses:** arbitrary shell interpreters are not used; executables run with allowlisted, validated argument lists.
- **Atomic reservations (`FileLockManager`):** all-or-nothing reservation of files and `mcp:` resources, with preventive lock retention when external process termination cannot be confirmed.

## Roadmap

### Phase 0 — v0.5.x core stabilization

- [x] Deterministic structural dependency validation and topological waves.
- [x] Stage-history persistence (`stage_log`) and initial resource-lock retention support.
- [ ] Audited external-process termination and reconciliation of retained locks.
- [ ] Experimental controlled parallel execution (`PARALLEL_EXECUTION=true`).

### Phase 1 — Domain-agnostic architecture and domain profiles

- [ ] Formalize the `DomainProfile` interface to fully decouple domain rules, prompts, and file conventions from the core engine.
- [ ] Extract modular profiles for software development, data pipelines, automation, and technical analysis.
- [ ] Make the A2A server domain-agnostic, driven by schemas and declared capabilities.

### Phase 2 — Multi-provider workspace and unified tools

- [ ] Fully abstract providers (OpenAI, Anthropic, Google, DeepSeek, and local inference through Ollama/vLLM) without changing orchestration workflows.
- [ ] Route tasks intelligently using capability, cost, and latency criteria.
- [ ] Build a unified permissions, execution, and tool-reservation gateway through the Model Context Protocol (MCP).
- [ ] Add local semantic context retrieval (RAG) for large documentation and codebases.

## Repository structure

```text
agents/          Model adapters, agent contracts, and quota management (Gemini, OpenCode, mocks)
api/             FastAPI REST routes, dependency injection, and application lifecycle
communication/   A2A server/client (a2a-sdk), Agent Cards, and structured messaging
config/          Dependency policies, assignment rules, and automated checks
core/            Environment settings, sanitized logging, and secure process management
database/        SQLite models (schema 6), additive migrations, and central repository
dashboard/       Static web operations interface (HTML/CSS/JS, no external dependencies)
mcp_bridge/      MCP server registry and health checks
memory/          Context management and auditable technical-decision repository
orchestrator/    Dependency Analyzer, workflow coordinator, lock manager, and schemas
scripts/         Test automation, validation, and startup scripts
tests/           Unit tests, integration tests, and mocks
tools/           Diagnostics, verification, and end-to-end test utilities
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
