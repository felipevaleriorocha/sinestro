"""Verifica o estado das integrações SEM consumir tokens (nenhum prompt é enviado aos modelos).

Uso: python -m tools.check_integrations [--json] [--orchestrator]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from agents.gemini_adapter import GeminiAdapter  # noqa: E402
from agents.opencode_adapter import OpenCodeAdapter  # noqa: E402
from core.config import BASE_DIR, load_settings  # noqa: E402
from core.logging_setup import redact, register_secrets  # noqa: E402
from mcp_bridge.server_registry import MCPServerRegistry  # noqa: E402

OK, WARN, FAIL = "OK", "AVISO", "FALHA"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--orchestrator", action="store_true", help="também consulta o orquestrador em execução")
    args = ap.parse_args()

    results: list[dict] = []

    def add(area: str, status: str, detail: str, data=None):
        results.append({"area": area, "status": status, "detail": redact(detail), "data": data})

    env_file = BASE_DIR / ".env"
    add(".env", OK if env_file.is_file() else FAIL, str(env_file) if env_file.is_file() else "Arquivo .env não encontrado.")
    s = load_settings()
    register_secrets(s.secrets())
    add("PROJECT_ROOT", OK if s.project_root.is_dir() else FAIL, str(s.project_root))
    add("Senha OpenCode", OK if s.opencode_password else WARN,
        "definida (valor oculto)" if s.opencode_password else "OPENCODE_SERVER_PASSWORD vazia — servidor sem autenticação?")

    g = GeminiAdapter(s)
    h = g.health(deep=True)
    add("Gemini / agy", OK if h.available else FAIL, h.detail, h.checks)

    oc = OpenCodeAdapter(s)
    try:
        h2 = oc.health(deep=True)
        st = OK if h2.available else (WARN if h2.status in {"quota_exceeded", "contract_not_verified"} else FAIL)
        add("DeepSeek / OpenCode", st, f"{h2.status}: {h2.detail}", h2.checks)
    finally:
        oc.close()

    mcp = MCPServerRegistry(s.mcp_config_file).status()
    add("MCP", OK if mcp else WARN, "configurado" if mcp else f"sem {s.mcp_config_file.name} (opcional nesta etapa)", mcp)

    if args.orchestrator:
        try:
            r = httpx.get(f"{s.public_url}/health", timeout=5)
            add("Orquestrador", OK if r.status_code == 200 else FAIL, f"HTTP {r.status_code} em {s.public_url}")
            for name in ("gemini", "deepseek"):
                c = httpx.get(f"{s.public_url}/a2a/{name}/.well-known/agent-card.json", timeout=5)
                add(f"Agent Card {name}", OK if c.status_code == 200 else FAIL,
                    c.json().get("name", "") if c.status_code == 200 else f"HTTP {c.status_code}")
        except httpx.HTTPError as exc:
            add("Orquestrador", FAIL, f"não respondeu em {s.public_url}: {type(exc).__name__}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        for r in results:
            print(f"[{r['status']:<6}] {r['area']}: {r['detail']}")
            if r["area"] == "DeepSeek / OpenCode" and isinstance(r["data"], dict):
                contract = r["data"].get("contract") or {}
                print(f"         OpenAPI: {contract.get('spec_source') or 'não exposta nas rotas candidatas'}")
                for name, route in (contract.get("routes") or {}).items():
                    if route:
                        print(f"         - {name:<17} {route['method']:<5} {route['path']:<48} {route['status']}")
                model = r["data"].get("model") or {}
                if model:
                    print(f"         modelo: found={model.get('found')} enabled={model.get('enabled')} variants={model.get('variants')}")
                print(f"         agentes OpenCode: {r['data'].get('opencode_agents')}")
            if r["area"] == "Gemini / agy" and isinstance(r["data"], dict):
                print(f"         binário: {r['data'].get('binary')}  wrapper .cmd: {r['data'].get('cmd_wrapper')}  "
                      f"flag esforço: {r['data'].get('effort_flag')}  flag retomada: {r['data'].get('resume_flag')}")
        print("\nNenhum prompt foi enviado aos modelos (sem consumo de tokens).")
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
