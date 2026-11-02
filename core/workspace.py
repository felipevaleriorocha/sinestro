"""Seleção e criação do diretório do projeto gerenciado (PROJECT_ROOT) em tempo de execução.

O orquestrador só escuta em loopback, então o servidor roda na máquina do usuário e pode abrir o explorador
de arquivos nativo dele. A janela é aberta em um subprocesso (tkinter não convive bem com o event loop do uvicorn).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


class WorkspaceError(ValueError):
    """Caminho ou nome de projeto inválido (vira HTTP 400)."""


class PickerUnavailable(RuntimeError):
    """Não há como abrir o seletor de pastas neste ambiente (sem tkinter ou sem tela)."""


_PICKER_SCRIPT = """
import sys
try:
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
except Exception:
    sys.exit(3)
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
initial = sys.argv[1] if len(sys.argv) > 1 else ""
path = filedialog.askdirectory(title="Selecione a pasta do projeto", initialdir=initial or None, mustexist=True)
root.destroy()
sys.stdout.write(path or "")
"""


def pick_folder(initial: Path | None = None, timeout: int = 600) -> str | None:
    """Abre o seletor de pastas nativo. Devolve o caminho escolhido, ou None se o usuário cancelar."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    args = [sys.executable, "-c", _PICKER_SCRIPT, str(initial) if initial else ""]
    try:
        res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PickerUnavailable(f"Seletor de pastas indisponível: {exc}") from exc
    if res.returncode == 3:
        raise PickerUnavailable("Seletor de pastas indisponível neste ambiente (tkinter ou tela ausente).")
    if res.returncode != 0:
        raise PickerUnavailable("Seletor de pastas indisponível: falha ao abrir a janela.")
    chosen = res.stdout.strip()
    return str(Path(chosen)) if chosen else None


def validate_project_dir(raw: str) -> Path:
    """Valida uma pasta existente escolhida como projeto. Recusa raiz do disco e a pasta pessoal inteira."""
    text = (raw or "").strip().strip('"')
    if not text:
        raise WorkspaceError("Informe o caminho da pasta do projeto.")
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        raise WorkspaceError("Use um caminho absoluto.")
    if not path.is_dir():
        raise WorkspaceError(f"A pasta não existe: {path}")
    path = path.resolve()
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise WorkspaceError("Escolha uma pasta de projeto, não a raiz do disco nem a pasta pessoal.")
    return path


def sanitize_project_name(name: str | None) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", name or "", flags=re.UNICODE).strip().replace(" ", "-")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-._")
    return cleaned[:60]


def create_project_dir(base: Path, name: str | None = None, now: datetime | None = None) -> Path:
    """Cria <base>/<nome> (nome sanitizado; vazio gera projeto-AAAAMMDD-HHMMSS). Nunca reaproveita uma pasta existente."""
    wanted = (name or "").strip()
    folder = sanitize_project_name(wanted)
    if wanted and not folder:
        raise WorkspaceError("O nome do projeto só pode ter letras, números, espaços, hífen e sublinhado.")
    if not folder:
        folder = "projeto-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    try:
        base.mkdir(parents=True, exist_ok=True)
        target = base / folder
        suffix = 2
        while target.exists():
            target = base / f"{folder}-{suffix}"
            suffix += 1
        target.mkdir()
    except OSError as exc:
        raise WorkspaceError(f"Não foi possível criar a pasta do projeto: {exc}") from exc
    return target.resolve()
