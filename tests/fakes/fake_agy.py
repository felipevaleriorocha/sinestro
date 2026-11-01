"""Simulador do Antigravity CLI para testes (MOCK — não chama nenhum modelo).

Comportamento controlado por variáveis de ambiente:
  FAKE_AGY_MODE = success | error_status | invalid_json | quota | sleep | nonzero | noisy | empty
  FAKE_AGY_HELP = lista de flags anunciadas em --help (padrão: "-p --model --output-format")
  FAKE_AGY_RESPONSE = texto da resposta (padrão: eco dos argumentos)
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if "--help" in args:
    print("Usage: agy [options]\n" + "\n".join(f"  {f}  ..." for f in os.environ.get("FAKE_AGY_HELP", "-p --model --output-format").split()))
    sys.exit(0)

mode = os.environ.get("FAKE_AGY_MODE", "success")
response = os.environ.get("FAKE_AGY_RESPONSE") or json.dumps({"args": args})
payload = {"conversation_id": "conv-123", "status": "SUCCESS", "response": response, "duration_seconds": 1.5,
           "num_turns": 2, "usage": {"input_tokens": 10, "output_tokens": 5}}

if mode == "sleep":
    time.sleep(30)
elif mode == "error_status":
    payload.update(status="ERROR", response="", error="model crashed")
elif mode == "quota":
    payload.update(status="ERROR", response="", error="RESOURCE_EXHAUSTED: quota exceeded for model")
elif mode == "invalid_json":
    print("isto não é json")
    sys.exit(0)
elif mode == "nonzero":
    print("fatal: not logged in", file=sys.stderr)
    sys.exit(3)
elif mode == "empty":
    payload.update(response="")
elif mode == "permission_denied":
    payload.update(response="", denied_actions=[{"action": "command", "target": "bash"}])
elif mode == "noisy":
    print("[warn] update available")
print(json.dumps(payload))
