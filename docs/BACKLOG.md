# Backlog and first real-run measurements

[Português (Brasil)](BACKLOG.pt-BR.md)

Source: an external audit of commit `7de4854` (2026-11), re-checked against `main` at `d7876b4` on 2026-11-02, plus the first run with real agents. The audit is an outside opinion: items are verified against the code before being trusted. Status: **Fixed**, **Open**, **Decision** (needs the owner), **Not verified**.

## Audit items

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | CORS with `*` + credentials, `TrustedHost` with `*` | **Fixed** (`7ffffc5`) | `api/main.py` now uses an explicit origin list and no `*` host. `test_cors_middleware_headers` asserts a foreign `Origin` is not echoed. Still open: a token for state-changing methods (`POST/PATCH/DELETE`) and a test that rejects a foreign `Host`. |
| 2 | Gemini channel gate, API key leakage | **Fixed** (mostly) | `GEMINI_CHANNEL_ACK_RISK` exists, the key goes in `x-goog-api-key`, and `Settings.secrets()` includes it. Open: make the `api` channel refuse `implement_task` (it only returns text, so it cannot edit files). |
| 3 | Empty-stdout retry | **Fixed** | `"não retornou saída"` is now a retry marker in `agents/gemini_adapter.py`. No regression test for the retry yet. |
| 4 | Final review skipped silently as `passed` | **Fixed** | Simple mode records `skipped_by_policy` (`orchestrator/final_review.py`). |
| 5 | Single version source | **Fixed** | `core/version.py`; bumped to 0.5.2. |
| 6 | Resume must not reset counters | Open | The mutation that zeroes counters on resume survives the test suite. Add a regression test. |
| 7 | Cancel only reaches `implement_task` | Open | `last_a2a_task_id` is only stored for implementation, so an in-flight plan/review/final review keeps running after cancel. UI shows the cancel button only for active tasks. |
| 8 | 30-day pruning also deletes accepted `decisions` | Open | `Repository.prune_inactive_workflows` deletes decisions; keep accepted ones (they feed the context). |
| 9 | Routing: only runs on contestation, polluted samples | Open | Include the scoreboard when the proposal is accepted; exclude `blocked`/`canceled`/`skip_review` and double counts on resume; raise the minimum to ~8–10 samples; measure implementation time only; add low-risk exploration. |
| 10 | Quota badge uses invented token budgets | Open | `agent_registry.py` assumes 1.2M/500k tokens per 24 h; the real quota is weekly. Show real cooldown plus local weekly counters, labelled "estimated". |
| 11 | No live progress (`/activity`, stream-json/SSE) | Open | The dashboard still infers what each agent is doing. |
| 12 | `raw_text` still reaches the reviewer | Open | `model_dump()` of the implementation report includes it. |
| 13 | Terms of service for orchestrating `agy` | **Decision** | Forum answers conflict; the code never touches tokens or credentials. Decide before using `agy` inside the orchestrator long term. Not legal advice. |
| 14 | `agy -p` returning 0 bytes when stdout is not a TTY (Windows) | Not observed | Did not happen in the real run below (7 calls, 0 empty). Keep watching the empty-response rate. |

Not planned now (from the audit): more providers, RAG, parallel execution, ACP, generalizing to N agents, dollar estimates.

## Benchmark protocol (not run yet, except the first step)

Four arms — orchestrator full, orchestrator simple, `agy` solo, OpenCode solo — on 3 mechanical tasks and 1 feature of 2–3 subtasks, 3 repetitions each. Record calls per agent, total time, human interventions and pass/fail.

Decide the thresholds *before* running. Suggested: keep the orchestrator if, on the feature, it matches the best solo arm with ≤ 1.5× the time and ≤ 2× the calls; on mechanical tasks, ≤ 1.2× the calls of the best solo arm; autonomy ≥ 70 %; empty or malformed responses ≤ 5 % after retry. The routing scoreboard only pays for its complexity if first-pass improves after ~30 clean tasks.

## First real run (2026-11-02)

Real agents (Gemini via `agy`, DeepSeek via OpenCode) in a throwaway git folder, `simple` mode, auto-approve **off**, `selene` enabled. Two tiny Luau tasks, one sample each — this is an anecdote, not a benchmark.

| | Run 1: `Greeter.luau` (typed) | Run 2: `Farewell.luau` (untyped) |
|---|---|---|
| Outcome | `needs_human` | `completed`, first-pass approved |
| Wall time | 137 s | 111 s |
| Calls | 4 (plan, implement, implement again, review) | 3 (plan, implement, review) |
| Final review | not reached | `skipped_by_policy` |

Totals over the 7 calls: Gemini 4 calls (100.5k input / 15.0k output tokens, estimated US$ 0.20 — an estimate), DeepSeek 3 calls (free model). Gemini plan took 40–58 s, DeepSeek implementation 15–41 s, Gemini review 21–32 s. 0 empty responses, 0 quota errors.

### New findings

1. **Check false positive made the agent weaken the code.** In an empty project `selene` parsed the Luau annotation `greet(name: string): string` as 6 syntax errors, sent the task back to the implementer, and the implementer "fixed" it by deleting the types the task asked for. A failing check should not be able to push an agent below the requirement: configure `selene` for Luau/Roblox (`selene.toml`, `std = "roblox"`), enable `luau-analyze`, or skip the check when the project has no linter config.
2. **Review denied in headless mode.** With auto-approve off, Gemini's read-only review tried to run a command and `agy` refused (`permission_denied`); 1 of 2 reviews. The system escalated to `needs_human` correctly, but the cost was a whole task. Options: allow read-only commands for the review skill, retry once, or tell the user plainly that the sandbox needs auto-approve.
3. **Gemini input is ~25k tokens per call** on tiny tasks (CLI context overhead), so the cost driver is the number of `agy` calls, not the size of the task. Simple mode (3 calls) is the right default for mechanical work.
4. **Simple mode works as designed:** 3 calls, no negotiation, no final review, with an honest `skipped_by_policy`.

### Still to measure

Solo baselines (`agy`, OpenCode), full mode on a 2–3 subtask feature, quota failover mid-task, and `/metrics/routing-stats` after ~20–30 clean tasks.
