"""Checagens automáticas entre a implementação e a revisão por LLM.

- Sem shell; somente ferramentas da lista fixa ALLOWED_TOOLS; argumentos vêm do config com {files} substituído
  por caminhos validados dentro do workspace.
- Ferramenta ausente ou desligada => `skipped` com motivo (nunca quebra o workflow).
- Todas vêm DESLIGADAS em config/checks.json: confira as flags da sua instalação antes de ligar.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.logging_setup import redact
from core.safe_exec import resolve_binary, run_process
from orchestrator.file_locks import PathOutsideWorkspaceError, normalize_file

ALLOWED_TOOLS = frozenset({"selene", "stylua"})
MAX_OUTPUT = 3000


@dataclass
class CheckSpec:
    id: str
    binary: str
    args: list[str] = field(default_factory=lambda: ["{files}"])
    file_globs: list[str] = field(default_factory=lambda: ["*"])
    timeout_seconds: int = 120
    success_exit_codes: list[int] = field(default_factory=lambda: [0])
    enabled: bool = False


def _stem(binary: str) -> str:
    name = Path(binary).name.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


class ChecksRunner:
    def __init__(self, project_root: Path, config_file: Path | None = None, specs: list[CheckSpec] | None = None,
                 allow_unlisted: bool = False, runner: Callable = run_process,
                 config: dict | None = None, allowed_tools: frozenset[str] | None = None):
        """`allow_unlisted` existe só para testes (permite um checador falso); não é configurável pelo .env/config."""
        self.root = Path(project_root)
        self.config_file = Path(config_file) if config_file else None
        self._specs = specs
        self.allow_unlisted = allow_unlisted
        self._runner = runner
        self._config_data = config
        self._allowed_tools = allowed_tools

    def specs(self) -> tuple[list[CheckSpec], list[str]]:
        if self._specs is not None:
            return list(self._specs), []
        errors: list[str] = []
        if self._config_data is not None:
            data = self._config_data
        else:
            try:
                data = json.loads(self.config_file.read_text(encoding="utf-8")) if self.config_file else {}
            except (OSError, ValueError) as exc:
                return [], [f"config de checagens ilegível: {exc}"]
        out = []
        for raw in data.get("checks", []):
            try:
                spec = CheckSpec(id=str(raw["id"]), binary=str(raw.get("binary") or raw["id"]),
                                 args=[str(a) for a in raw.get("args", ["{files}"])],
                                 file_globs=[str(g) for g in raw.get("file_globs", ["*"])],
                                 timeout_seconds=int(raw.get("timeout_seconds", 120)),
                                 success_exit_codes=[int(c) for c in raw.get("success_exit_codes", [0])],
                                 enabled=bool(raw.get("enabled", False)))
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"checagem inválida {raw!r}: {exc}")
                continue
            out.append(spec)
        return out, errors

    def _allowed(self, spec: CheckSpec) -> bool:
        if self.allow_unlisted:
            return True
        allowed = self._allowed_tools
        if allowed is None:
            if self._config_data and "allowed_tools" in self._config_data:
                allowed = frozenset(self._config_data["allowed_tools"])
            else:
                allowed = ALLOWED_TOOLS
        return spec.id in allowed and _stem(spec.binary) in allowed

    def status(self) -> dict:
        specs, errors = self.specs()
        items = []
        for spec in specs:
            allowed = self._allowed(spec)
            path = resolve_binary(spec.binary) if allowed else None
            items.append({"id": spec.id, "binary": spec.binary, "enabled": spec.enabled, "allowed": allowed,
                          "installed": bool(path), "path": path, "active": bool(spec.enabled and allowed and path)})
        active = [i["id"] for i in items if i["active"]]
        return {"checks": items, "active": active, "any_active": bool(active), "errors": errors,
                "allowed_tools": sorted(ALLOWED_TOOLS)}

    def _target_files(self, spec: CheckSpec, files: list[str]) -> list[str]:
        out = []
        seen: set[str] = set()
        for f in sorted(set(files)):
            try:
                key = normalize_file(self.root, f)  # trava: recusa caminhos fora do workspace
            except PathOutsideWorkspaceError:
                continue
            if key in seen:
                continue
            seen.add(key)
            rel = posixpath.normpath(str(f).replace("\\", "/"))  # preserva maiúsculas/minúsculas originais
            if (self.root / rel).is_file() and any(fnmatch.fnmatch(Path(rel).name, g) for g in spec.file_globs):
                out.append(rel)
        return out

    def run(self, files: list[str]) -> dict:
        specs, errors = self.specs()
        results = []
        for spec in specs:
            base = {"id": spec.id}
            if not spec.enabled:
                results.append({**base, "status": "skipped", "reason": "desligada em config/checks.json"})
                continue
            if not self._allowed(spec):
                results.append({**base, "status": "skipped", "reason": "ferramenta fora da lista permitida"})
                continue
            binary = resolve_binary(spec.binary)
            if not binary:
                results.append({**base, "status": "skipped", "reason": f"'{spec.binary}' não instalado/no PATH"})
                continue
            targets = self._target_files(spec, files)
            if not targets:
                results.append({**base, "status": "skipped", "reason": "nenhum arquivo correspondente alterado"})
                continue
            args: list[str] = []
            for a in spec.args:
                args.extend(targets if a == "{files}" else [a])
            try:
                res = self._runner([binary, *args], cwd=self.root, timeout=spec.timeout_seconds)
            except OSError as exc:
                results.append({**base, "status": "skipped", "reason": f"falha ao iniciar: {exc}"})
                continue
            output = redact(((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip())[-MAX_OUTPUT:]
            if res.timed_out:
                results.append({**base, "status": "error", "reason": f"timeout de {spec.timeout_seconds}s", "files": targets})
            elif res.returncode in spec.success_exit_codes:
                results.append({**base, "status": "passed", "files": targets})
            else:
                results.append({**base, "status": "failed", "exit_code": res.returncode, "files": targets, "output": output})
        failed = [r for r in results if r["status"] == "failed"]
        ran = [r for r in results if r["status"] in {"passed", "failed", "error"}]
        overall = "failed" if failed else ("passed" if ran else "no_active_checks")
        signature = hashlib.sha256(json.dumps([(r["id"], r.get("output", "")) for r in failed], ensure_ascii=False)
                                   .encode("utf-8")).hexdigest()[:16] if failed else None
        return {"status": overall, "results": results, "failures": failed, "signature": signature, "config_errors": errors}
