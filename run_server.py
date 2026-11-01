"""Inicia o orquestrador com host/porta do .env (ORCH_HOST/ORCH_PORT), garantindo que as URLs A2A batam com a porta real."""
from __future__ import annotations

import sys

import uvicorn

from core.config import load_settings


def main() -> int:
    s = load_settings()
    if s.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"Recusado: ORCH_HOST={s.host}. O orquestrador só escuta em loopback.", file=sys.stderr)
        return 2
    uvicorn.run("app:app", host=s.host, port=s.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
