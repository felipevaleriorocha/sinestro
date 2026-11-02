"""Logging com redação de segredos (senha do OpenCode, cabeçalhos Authorization)."""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

_AUTH_RE = re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?)(Basic|Bearer)\s+[A-Za-z0-9+/=._-]+", re.IGNORECASE)
_URL_CRED_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
_KV_RE = re.compile(r"((?:PASSWORD|SENHA|TOKEN|SECRET)[A-Z_]*\s*[=:]\s*)\S+", re.IGNORECASE)

_secrets: set[str] = set()


def register_secrets(values: Iterable[str]) -> None:
    for v in values:
        if v and len(v) >= 4:
            _secrets.add(v)


def redact(text: object) -> str:
    """Remove segredos conhecidos de qualquer texto antes de logar/persistir/expor."""
    s = str(text)
    for secret in _secrets:
        s = s.replace(secret, "***")
    s = _AUTH_RE.sub(r"\1\2 ***", s)
    s = _URL_CRED_RE.sub(r"\1***:***@", s)
    s = _KV_RE.sub(r"\1***", s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover  # noqa: BLE001
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_orch", False) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_dir / "orchestrator.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(RedactingFilter())
    file_handler._orch = True  # type: ignore[attr-defined]
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactingFilter())
    console._orch = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    root.addHandler(console)
    root.setLevel(level)
    # httpx loga URLs em INFO; mantemos em WARNING para não poluir.
    logging.getLogger("httpx").setLevel(logging.WARNING)
