# Backlog and first real-run measurements

[Português (Brasil)](BACKLOG.pt-BR.md)

Source: an external audit of commit `7de4854` (2026-11), re-checked against `main` at `d7876b4` on 2026-11-02, plus the first run with real agents. The audit is an outside opinion: items are verified against the code before being trusted. Status: **Fixed**, **Open**, **Not verified**.

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
| 13 | `agy -p` returning 0 bytes when stdout is not a TTY (Windows) | Not observed | Did not happen in the real run below (7 calls, 0 empty). Keep watching the empty-response rate. |

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

## Scenario 1: full orchestrator vs. each agent alone (2026-11-02, 1 sample)

Same task for every arm: three small Luau modules (`Stack`, `Queue`, and `Collections`, which depends on the other two), no type annotations. Fresh git folder per run, same models (`gemini-3.8-flash-high`, `deepseek-v4.1-flash-free`). `agy` ran with `--dangerously-skip-permissions` **only for this benchmark** (see finding 1). One sample per arm, so treat it as a strong anecdote, not statistics.

| | Orchestrator, full mode | `agy` alone | OpenCode alone |
|---|---|---|---|
| Wall time | 544 s | 94 s | 21 s |
| Calls | 10 (Gemini 6, DeepSeek 4) | 1 | 1 (6 tool steps) |
| Tokens in / out | Gemini 291k / 42k, DeepSeek 5k / 2k | 129k (118k cache) / 7k | ~81k (incl. cache) / 1k |
| Estimated cost | US$ 0.57 (estimate) | n/a | free model |
| Human interventions | 0 | 0 | 0 |
| Result | 3 files, 16/16 API checks, final review `passed` | 3 files, 16/16 | 3 files, 16/16 |
| `selene` | 0 errors (agent added `--# selene: allow(undefined_variable)`) | 2 errors (`script` undefined) | 2 errors (`script` undefined) |

I read all nine files: the logic is equivalent and correct in every arm (empty `pop`/`peek`/`dequeue` return `nil`, `reverse` builds a new list through a `Stack` and does not mutate its input, each module returns its table).

**Reading against the decision rule** (≤ 1.5× time and ≤ 2× calls of the best solo arm for a feature): the orchestrator took about 26× the time of OpenCode alone (5.8× of `agy` alone) and 10× the calls, for the same quality. On a task this small it does not pay. This says nothing yet about harder or riskier work, where cross-review might catch real defects; here there were none to catch.

Where the 544 s went: Gemini ≈ 516 s (plan 99 s, `T3` implementation 243 s + 63 s fix, three reviews 19–39 s, final review 47 s), DeepSeek ≈ 56 s (two implementations of 14 s each, one review of 19 s, one assignment answer of 9 s). The subtask assigned to Gemini cost more than everything DeepSeek did.

### Findings

1. **`agy` in headless mode cannot finish an implementation without `--dangerously-skip-permissions`.** In the pilot it was denied 3 times out of 3: a shell command (solo, `accept-edits` mode), an MCP action (as implementer inside the orchestrator) and a command (review, earlier run). The solo call even returned `status: SUCCESS` with an empty response. This is the real reliability limit of the `agy` channel today.
2. **`opencode run` takes its working directory from the `PWD` environment variable, not from the process directory.** Started from a shell whose `PWD` was the project repository, it wrote files there. The orchestrator is not affected (it passes `location.directory` explicitly), but any script that launches `opencode run` must set `PWD`.
3. **The `selene` false positive is back** (`script` is not defined, the Roblox global). The implementer silenced it with an inline `allow` comment. Same fix as the earlier finding: configure `selene` with the Roblox standard library.
4. **Assignment policy matters more than the number of calls.** Routing the implementation to DeepSeek and keeping Gemini for planning and review would have cut roughly half of the time on this task.

### Next

Run the failover scenario (quota exhausted mid-task). Optionally repeat scenario 1 with a harder, defect-prone feature, where the review stage has something to find, and with 2 more repetitions.

## Scenario 2: a harder, defect-prone feature (2026-11-02, 2 repetitions per arm)

The test suggested at the end of Scenario 1: five Luau modules with precise edge-case rules (`Money` in integer cents, `Inventory` with stack limits and all-or-nothing removal, `Shop` with a fixed check order, `Cooldown` with an inclusive boundary, `Serializer` with strict validation). Correctness is measured by a **hidden suite of 46 checks** run in a Lua 5.1 interpreter (with a small Luau compatibility layer: `typeof`, generalized `for ... in t`, compound assignment). The suite passes 46/46 on my reference implementation and fails exactly the expected checks on four injected defects. Same models and permissions as Scenario 1 (`agy` with `--dangerously-skip-permissions` **only for this benchmark**).

| Arm | Wall time (run 1 / run 2) | Calls | Hidden suite | Interventions |
|---|---|---|---|---|
| Orchestrator, full mode | 909 s / 1775 s | 13 / 15 | **36/46 and 36/46** | 1 of 2 (`needs_human`) |
| `agy` alone | 470 s / 426 s | 1 | 46/46 and 46/46 | 0 |
| OpenCode alone | 151 s / 101 s | 1 | 46/46 and 46/46 | 0 |

Tokens: orchestrator 281k / 93k and 562k / 193k in/out (estimated US$ 0.77 and 1.60); `agy` alone 142-218k in plus ~0.9-1.5M cached, ~50k out plus ~45k thinking; OpenCode alone 236k-540k in (mostly cache), 3-11k out. The third repetition was **not run**: the pattern was already consistent and it would have cost ~40 minutes and another ~500k Gemini tokens; the in-flight workflow was cancelled during planning.

### What happened

1. **The orchestrator shipped the same defect twice.** DeepSeek wrote `Money.fromString` with the pattern `^%d+(%.%d%d?)?$`; Lua and Luau patterns do not support `?` after a group, so the pattern never matches and every valid amount is rejected. `Shop` breaks in cascade (10 of 46 checks). The same models alone, with the whole specification in one prompt, did not make this mistake in 4 of 4 runs.
2. **Review caught it only once.** Run 1: five approvals and a final review `passed`, with the defect in place. Run 2: the final review (Gemini) identified the `Money`/`Shop` integration failure, and the workflow stopped in `needs_human`. Reviewers read code, nobody executes it, and `selene` cannot see a pattern-semantics error.
3. **No automatic repair after a failed final review.** Run 2 ended in `needs_human` instead of sending the finding back to the implementer.
4. **Time is dominated by Gemini implementations**: 385 s in run 1 and 1311 s over four calls in run 2 (one subtask needed a correction cycle). DeepSeek implementations took 11-25 s each.

### Reading against the decision rule

The orchestrator misses every threshold: about 11x the time of OpenCode alone (3x that of `agy`) and 14x the calls, **worse** quality (36/46 vs 46/46), and autonomy 50 % (1 of 2 runs needed a human). The hypothesis that cross-review adds value was not supported here: the solos had nothing to fix, and the orchestrator introduced a defect that its review stage caught half of the time. This is two repetitions of one feature, so it is strong evidence for this kind of task, not a universal verdict.

### Implications for the roadmap

- Do not invest further in orchestration logic (routing, negotiation, more agents) before something else changes the picture.
- If the project keeps going, the missing piece is **execution, not more reading**: an executable check (a Luau runtime running tests the implementer writes or the spec provides) and an automatic fix loop after a failed final review.
- Make the single-agent path the default, and escalate to the full flow only for risky changes.

### Harness lessons (not product bugs)

`opencode.cmd` goes through `cmd.exe`, which truncates the prompt at the first newline (flatten it, avoid the characters `" < > | & ^ %`). Models write idiomatic Luau (`typeof`, `for k, v in t do`), so a plain Lua 5.1 runner scores them unfairly; use a Luau-aware runner. The first OpenCode run of the pilot received a truncated prompt and was discarded.
