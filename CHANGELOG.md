# Changelog

[Português (Brasil)](CHANGELOG.pt-BR.md)

## 0.5.2 — Portfolio polish and mock-mode fixes
- **Optional project (`PROJECT_ROOT`):** the project can be picked from the dashboard (**Open folder…** opens the native file explorer) or created under `DEFAULT_PROJECTS_DIR` (default `~/Documents/Sinestro`); the choice is saved to `.env`. New endpoints `GET /workspace`, `POST /workspace/pick|select|create`; `POST /tasks` answers `409 project_root_required` until a project exists.
- **Mock mode from `.env`:** `ORCH_MOCK_AGENTS` was only read from the shell environment; it is now read from `.env` (`Settings.mock_agents`), with a regression test.
- **Dashboard:** agent cards use `minmax(0,1fr)` and no longer overflow the right edge on wide screens; model names are shown in full; the page title is now "SINESTRO".
- **CI and quality:** GitHub Actions (ruff + pytest on Python 3.11 and 3.12), `pyproject.toml`, coverage with `pytest-cov`; justified `# noqa: BLE001` on boundary `except Exception` blocks.
- **Layout:** `diagnostico_dependencias.py` moved to `tools/`; unified `.gitignore`; `.env.example` without a personal path.
- **Documentation:** mock-mode quickstart, design decisions, Mermaid diagram, aligned roadmap, demo GIF, `docs/DEMO.md`, startup-script reference, and English-first docs with `*.pt-BR.md` counterparts.

## 0.5.1 — Permission management and prior human approval
- **Configurable auto-approval (`AGY_AUTO_APPROVE_PERMISSIONS`):** support for the `--dangerously-skip-permissions` flag in the `agy` CLI. By default (`false`) the agent does not run actions with elevated permissions without authorization.
- **Prior approval (`requires_approval` / `requires_permission`):** subtasks that need system commands or execution privileges pause in `needs_human` before starting, avoiding non-interactive refusals.
- **Permission-error recovery:** when a call fails with a permission denial from `agy` (`AgentPermissionError` / `permission_denied`), the subtask moves to `needs_human` with a detailed notice on the dashboard instead of breaking the workflow.
- **Dashboard decision flow:** clicking `approve` on a subtask with no previous result grants permission (`allow_permissions=True`), returns it to `pending` and injects the authorization on resume (`POST /tasks/{id}/resume`).
- **Visual identity:** the DeepSeek logo on the dashboard now uses the official icon (blue whale).
- **Launcher (`start.bat`):** dynamic relative path (`%~dp0`) so startup works from any install directory.

## 0.5.0 — Architecture evolution: multi-provider workspace
- **Phase 0 (core stabilization):** strict, centralized lock retention when a process cannot be confirmed as terminated (timeout), a sound `stage_log` lifecycle and isolated injection of the dependency policy (`dependency_policy.json`).
- **Phase 1 (passive domain extraction):** `DomainProfile` interface (`core/domain.py`) and the `roblox` domain decoupled from the core; the core is fully language- and framework-agnostic.
- **Phase 2 (provider abstraction):** `Provider`, `ModelSpec`, `ModelCapability` and `CapabilityRegistry` entities separated from agents and concrete backends.
- **Phase 3 (dynamic workflow engine):** `WorkflowDAG` and `WorkflowEngine` with wave-based topological ordering, flexible execution and stage skipping (e.g. no cross-review for assets).
- **Phase 4 (intelligent routing and unified workspace):** local hybrid RAG on SQLite (`VectorStore`), automatic workspace indexing, cost/capability task router and a consolidated cost report on the dashboard.

## 0.4.2
- Contracts accept `null` in optional fields (previously an approved review with `"rejection_reason": null` was treated as unstructured and cost an extra review).
- Reformulation distinguishes FORMAT errors from EVIDENCE errors; no synthetic "did not return JSON" finding.
- Dashboard: updates without losing scroll/selection (`setHTML`); `docs/FRONTEND.md`.
- Checks receive paths with their original capitalization.
- `Read-EnvFile` accepts a relative path.
- Cards and prompts no longer hard-code the model name (the model comes from `.env`).

## 0.4.1 — a final review without gaps
- `final_review_state` is always persisted (passed, disabled, skipped, failed, changes_requested, rejected, unverified, invalidated, not_reached, accepted_by_human, rejected_by_human) and the decision is centralized in the pure function `final_status()`.
- New status `completed_unreviewed`: subtasks approved but no final review. It never becomes a clean `completed`.
- The final reviewer is never the agent that implemented everything; in the mixed case, Gemini applies an asymmetric rule, with `self_authored_subtasks` and per-subtask coverage in the result.
- Final review focused on integration, with evidence, a single reformulation, hash comparison and read-only mode.
- Resuming from `completed_unreviewed`/`needs_human` runs only the final review (attempt + snapshot; event if the workspace changed). `POST /tasks/{id}/final-review/decision` (accept|reject). A negative verdict moves to `needs_human`.
- Dashboard: "final review disabled" badge, "no final review" warning, final review section. Schema 5 (backup `.bak-v4`).

## 0.4.0 — symmetric roles
- Both agents have the same A2A skills: `plan_feature`, `respond_assignment`, `implement_task`, `review_implementation` and `final_review`. **Whoever implements never reviews**: the reviewer is computed as the other agent, validated in the contract (`AssignmentDecision`), in the repository and in the coordinator (`implemented_by`).
- Two-turn assignment: proposal in the plan, accept/object in a single call and deterministic tie-break from `config/assignment_rules.json` (configurable `default_agent`). Gemini out of quota -> DeepSeek takes over (`quota_fallback`).
- Pluggable automated checks (`config/checks.json`, fixed list, no shell, disabled by default). A failure goes back to the implementer without an LLM review; an identical failure escalates immediately.
- Review with checklist and evidence (`ReviewFinding`: file, line and excerpt within ±5 lines). A single reformulation; after that, `review_unverified` -> `needs_human`. Changing files during a review invalidates the verdict.
- Separate counters: `review_cycles`, `check_fix_attempts` and `impl_attempts` (global cap).
- New states: subtask `awaiting_review` / `needs_human`; workflow `needs_human` / `paused`. `POST /tasks/{id}/subtasks/{sid}/decision` + `resume`.
- A plan produced by fallback is validated by Gemini on the first review. Dependents only start once the dependency is approved. Pauses when `MAX_BLOCKED_SUBTASKS` is reached.
- Per-workflow metrics: objections, who objected, the winner and the tie-break rule, how many times negotiation changed the outcome, and calls per agent/skill.
- Adapters: agy with `--mode plan` on read-only skills and `--add-dir` for file prompts (both detected through `--help`); quota cooldown on Gemini too; OpenCode with the `Plan` agent for review, in a separate session.
- Database schema 4 with automatic `orchestrator.db.bak-v<N>` backups.
- Dashboard: Implemented by / Reviewed by / Reason columns, counters, human-decision buttons and a "no check enabled" notice.
- `run_server.py` with `access_log=False` and `Iniciar Orquestrador.bat` (separate commit).

## 0.3.0
A2A with the official SDK, AGY/OpenCode 2.x adapters, review by Gemini, shared memory, file reservations, MCP groundwork, dashboard and tests (details in docs/AUDITORIA_v0.2.md).
