"""Execução controlada de comandos do sistema.

Nenhum endpoint aceita comandos arbitrários. Apenas binários e subcomandos
explicitamente permitidos aqui podem ser executados, sempre sem shell.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Somente leitura: o orquestrador não faz commit/checkout/reset no repositório do jogo.
GIT_ALLOWED = {"status", "rev-parse", "diff", "log", "ls-files", "--version"}


class CommandNotAllowed(RuntimeError):
    pass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def resolve_binary(name_or_path: str) -> str | None:
    """Resolve um executável respeitando PATHEXT no Windows (agy -> agy.cmd/agy.exe)."""
    p = Path(name_or_path)
    if p.is_file():
        return str(p)
    return shutil.which(name_or_path)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Encerra o processo e seus filhos (no Windows, .cmd -> node fica órfão sem /T)."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - encerramento best-effort do processo
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - encerramento best-effort do processo
        pass


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        # Grupo próprio: Ctrl+C no terminal do servidor não derruba o agy; o encerramento é feito por kill_process_tree.
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def run_process(
    command: Sequence[str],
    cwd: Path | None,
    timeout: float,
    stdin_text: str | None = None,
    cancel_event: threading.Event | None = None,
    on_start=None,
) -> CommandResult:
    """Executa um processo sem shell, com timeout e cancelamento que matam a árvore inteira."""
    proc = subprocess.Popen(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    if on_start:
        on_start(proc)
    result: dict = {}

    def _communicate() -> None:
        try:
            out, err = proc.communicate(input=stdin_text)
            result["out"], result["err"] = out, err
        except Exception as exc:  # pragma: no cover  # noqa: BLE001
            result["out"], result["err"] = "", str(exc)

    t = threading.Thread(target=_communicate, daemon=True)
    t.start()
    waited = 0.0
    step = 0.2
    timed_out = False
    while t.is_alive():
        t.join(step)
        waited += step
        if cancel_event is not None and cancel_event.is_set():
            kill_process_tree(proc)
            t.join(5)
            return CommandResult(-2, result.get("out", ""), "cancelado", False)
        if waited >= timeout:
            timed_out = True
            kill_process_tree(proc)
            t.join(5)
            break
    return CommandResult(proc.returncode if proc.returncode is not None else -1, result.get("out", "") or "", result.get("err", "") or "", timed_out)


def run_git(args: Sequence[str], repo: Path, timeout: float = 30) -> CommandResult:
    if not args or args[0] not in GIT_ALLOWED:
        raise CommandNotAllowed(f"Subcomando git não permitido: {args[:1]}")
    git = resolve_binary("git")
    if not git:
        return CommandResult(127, "", "git não encontrado no PATH")
    return run_process([git, "-c", "core.quotepath=off", *args], cwd=repo, timeout=timeout)


def process_running(image_name: str, timeout: float = 15) -> bool | None:
    """Verifica se um processo (ex.: RobloxStudioBeta.exe) está ativo. None = não verificável."""
    if not image_name or any(c in image_name for c in "&|<>;\"'"):
        raise CommandNotAllowed("Nome de processo inválido.")
    if IS_WINDOWS:
        res = run_process(["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"], cwd=None, timeout=timeout)
        return image_name.lower() in res.stdout.lower()
    if sys.platform.startswith("linux"):
        pgrep = resolve_binary("pgrep")
        if not pgrep:
            return None
        res = run_process([pgrep, "-f", image_name], cwd=None, timeout=timeout)
        return res.returncode == 0
    return None
