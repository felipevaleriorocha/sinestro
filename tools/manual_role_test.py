"""Teste REAL manual dos papéis (CONSOME COTA). Não é executado pela suíte automática.

  python -m tools.manual_role_test --implementer deepseek   # DeepSeek implementa, Gemini revisa
  python -m tools.manual_role_test --implementer gemini     # Gemini implementa, DeepSeek revisa

Para gastar o mínimo: plano fixo (sem chamada de planejamento), sem negociação de atribuição, sem revisão final,
no máximo 1 ciclo de ajuste. Custo esperado: 1 implementação + 1 a 3 revisões (inclui uma possível reformulação).

Roda num workspace de RASCUNHO (runtime/role_test_ws/<implementador>), não no jogo. O DeepSeek recebe esse diretório
em `location.directory` ao criar a sessão do OpenCode; se o seu OpenCode ignorar esse campo, ele pode editar o
diretório onde o servidor foi iniciado. Por isso o script compara o `git status` do PROJECT_ROOT real antes/depois e avisa.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.main import create_app, shutdown, startup  # noqa: E402
from core.config import BASE_DIR, load_settings  # noqa: E402
from core.safe_exec import run_git  # noqa: E402

TASK = {
    "id": "T1",
    "category": "mechanical",
    "assignment_reason": "teste manual de papéis",
    "title": "Módulo RoleTest com soma validada",
    "objective": "Criar src/shared/RoleTest.lua: ModuleScript que retorna uma tabela com a função add(a, b).",
    "instructions": "add(a, b) retorna a + b. Se a ou b não for number, chame error com mensagem clara. Use Luau com --!strict.",
    "files": ["src/shared/RoleTest.lua"],
    "acceptance_criteria": ["O módulo retorna uma tabela com add", "add(2, 3) == 5", "add('x', 1) gera erro"],
    "constraints": ["Não criar outros arquivos", "Não usar serviços do Roblox"],
    "depends_on": [],
}


def git_status(root: Path) -> str | None:
    try:
        res = run_git(["status", "--porcelain=v1"], root, timeout=30)
        return res.stdout if res.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


async def main(implementer: str, keep: bool) -> int:
    base = load_settings()
    real_root = base.project_root
    ws = BASE_DIR / "runtime" / "role_test_ws" / implementer
    if ws.exists() and not keep:
        shutil.rmtree(ws)
    (ws / "src" / "shared").mkdir(parents=True, exist_ok=True)
    (ws / "AGENTS.md").write_text("# AGENTS (rascunho de teste)\n- Luau com --!strict.\n- Não crie arquivos fora do pedido.\n", encoding="utf-8")
    (ws / "README.md").write_text("# Workspace de teste do orquestrador (não é o jogo)\n", encoding="utf-8")
    before = git_status(real_root) if real_root.is_dir() else None

    s = load_settings(overrides={
        "PROJECT_ROOT": str(ws), "ORCH_DB_PATH": str(BASE_DIR / "runtime" / "role_test.db"),
        "ASSIGNMENT_NEGOTIATION": "false", "FINAL_REVIEW": "false", "MAX_REVIEW_CYCLES": "1",
        "MAX_IMPLEMENTATION_ATTEMPTS": "2",
    })
    app = create_app(s, a2a_in_process=True)
    await startup(app)
    c = app.state.container
    try:
        wf = c.tasks.create_workflow(f"[TESTE MANUAL] {implementer} implementa", "full")
        plan = {"summary": "teste manual", "tasks": [{**TASK, "assigned_to": implementer, "agent": implementer}],
                "decisions": [], "risks": [], "planned_by": "gemini", "planned_by_fallback": False, "plan_validated_by": None}
        c.repo.update_workflow(wf, plan_json=plan, planned_by="gemini")
        c.tasks.create_subtasks(wf, plan["tasks"])
        print(f"Workflow {wf} — implementa: {implementer}; revisa: {'gemini' if implementer == 'deepseek' else 'deepseek'}")
        print(f"Workspace de rascunho: {ws}")
        c.coordinator.start(wf, resume=True)
        t0 = time.monotonic()
        while c.coordinator.is_running(wf):
            await asyncio.sleep(3)
            sub = c.tasks.subtasks(wf)[0]
            print(f"  [{int(time.monotonic() - t0):>4}s] {sub['status']}")
        d = c.repo.get_workflow(wf)
        sub = c.tasks.subtasks(wf)[0]
        print("\n=== RESULTADO ===")
        print(json.dumps({"workflow": d["status"], "subtask": sub["status"], "implementou": sub.get("assigned_to"),
                          "revisou": sub.get("reviewer"), "ajustes": sub.get("review_cycles"), "tentativas": sub.get("impl_attempts"),
                          "veredito": (sub.get("review") or {}).get("verdict"), "erro": sub.get("error"),
                          "escalonamento": (sub.get("escalation") or {}).get("code"),
                          "chamadas": (d.get("metrics") or {}).get("calls_per_agent")}, ensure_ascii=False, indent=2))
        out = ws / "src" / "shared" / "RoleTest.lua"
        print(f"\nArquivo gerado: {out} ({'existe' if out.exists() else 'NÃO existe'})")
        print("Histórico completo: abra o painel apontando ORCH_DB_PATH para runtime\\role_test.db, ou consulte o banco.")
    finally:
        await shutdown(app)
    after = git_status(real_root) if real_root.is_dir() else None
    if before is not None and after is not None and before != after:
        print(f"\n!!! ATENÇÃO: o git status do jogo ({real_root}) mudou durante o teste. Confira com `git status` —"
              " o OpenCode pode ter ignorado location.directory.")
        return 2
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--implementer", choices=["deepseek", "gemini"], required=True)
    ap.add_argument("--keep", action="store_true", help="não apaga o workspace de rascunho anterior")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.implementer, a.keep)))
