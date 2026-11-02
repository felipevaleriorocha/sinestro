# Guided demo (mock mode)

[Português (Brasil)](DEMO.pt-BR.md)

A ~45-second walkthrough that shows SINESTRO working without `agy`, OpenCode or tokens. Use it to explore the project or to record the README GIF.

> Nothing in this mode is a real integration: the agents are simulated (`agents/mock_adapters.py`). The demo shows the **orchestration flow** (plan, DAG, assignment, cross-review, final review).

## 1. Prepare

```powershell
if (!(Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

> Use `python -m pip` instead of `pip.exe`: if the project folder was copied from somewhere else, the `pip.exe` launcher inside `.venv` still points to the old path and fails with "Fatal error in launcher". If that happens, delete `.venv` (with the server stopped) and recreate it.
> The `if (!(Test-Path .env))` guard avoids overwriting an `.env` you already configured.

In `.env`, set:

```
PROJECT_ROOT=C:\path\to\an\empty\folder   # the folder must already exist; create it first
ORCH_MOCK_AGENTS=true
```

## 2. Start

```powershell
.\.venv\Scripts\python run_server.py
```

- Dashboard: <http://127.0.0.1:8787/dashboard/>
- API: <http://127.0.0.1:8787/docs>

The log should print `mock=True`. If it prints `mock=False`, stop the server (`Ctrl+C`) and check the `.env`.

## 3. Submit a task

From the dashboard, or:

```powershell
curl.exe -X POST http://127.0.0.1:8787/tasks -H "Content-Type: application/json" -d "{\"goal\": \"Create a hello-world module\", \"mode\": \"full\"}"
```

## 4. What to show in the GIF (suggested script)

1. **0-5 s:** empty dashboard with both agent cards.
2. **5-15 s:** submit the goal; the plan appears with its subtasks.
3. **15-30 s:** DAG and assignment; show that whoever implements does not review.
4. **30-40 s:** cross-review and automated checks.
5. **40-45 s:** `final_review_state: passed` and status `completed`.

## 5. Record

Free tools: [ScreenToGif](https://www.screentogif.com/) (Windows) or [LICEcap](https://www.cockos.com/licecap/). Save it as `docs/media/demo.gif` (keep it under ~5 MB); it is already referenced at the top of the READMEs.
