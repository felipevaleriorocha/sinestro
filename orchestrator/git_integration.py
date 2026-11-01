"""Integração Git somente leitura: snapshot antes/depois para atribuir alterações a cada subtarefa.

O orquestrador NÃO faz commit, checkout, reset ou push no repositório do jogo.
Sem Git disponível, cai para snapshot por hash dos arquivos reservados/relatados.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from core.safe_exec import run_git


def _sha(path: Path) -> str | None:
    try:
        h = hashlib.sha1()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@dataclass
class Snapshot:
    is_git: bool
    head: str | None
    files: dict[str, str | None] = field(default_factory=dict)  # caminho -> hash (None = inexistente)


class GitInspector:
    def __init__(self, project_root: Path):
        self.root = Path(project_root)

    def is_repo(self) -> bool:
        if not self.root.is_dir():
            return False
        try:
            res = run_git(["rev-parse", "--is-inside-work-tree"], self.root, timeout=15)
        except Exception:
            return False
        return res.returncode == 0 and res.stdout.strip() == "true"

    def head(self) -> str | None:
        res = run_git(["rev-parse", "HEAD"], self.root, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else None

    def dirty_files(self) -> list[str]:
        res = run_git(["status", "--porcelain=v1", "-uall", "-z"], self.root, timeout=60)
        if res.returncode != 0:
            return []
        out = []
        entries = res.stdout.split("\0")
        i = 0
        while i < len(entries):
            e = entries[i]
            if len(e) > 3:
                status, path = e[:2], e[3:]
                out.append(path)
                if "R" in status or "C" in status:
                    i += 1  # próximo item é o caminho de origem
            i += 1
        return out

    def snapshot(self, extra_paths: list[str] | None = None) -> Snapshot:
        extra_paths = extra_paths or []
        is_git = self.is_repo()
        paths = set(extra_paths)
        head = None
        if is_git:
            head = self.head()
            paths |= set(self.dirty_files())
        return Snapshot(is_git, head, {p: _sha(self.root / p) for p in paths})

    def changes_since(self, before: Snapshot, extra_paths: list[str] | None = None) -> list[dict]:
        after = self.snapshot(list(before.files.keys()) + list(extra_paths or []))
        changes = []
        for path in sorted(set(before.files) | set(after.files)):
            a = after.files.get(path)
            if path in before.files:
                b = before.files[path]
                if a == b:
                    continue
                kind = "added" if b is None else ("deleted" if a is None else "modified")
            else:
                # Estava limpo antes e agora aparece no `git status`: mudou durante esta etapa.
                kind = "deleted" if a is None else "modified_or_added"
            item = {"path": path, "change_type": kind}
            # Diff isolado por arquivo. Arquivos novos não rastreados não aparecem em git diff,
            # então o conteúdo atual é incluído de forma limitada.
            if before.is_git:
                diff = run_git(["diff", "--", path], self.root, timeout=30)
                text = diff.stdout if diff.returncode == 0 else ""
            else:
                text = ""
            if not text and a is not None:
                try:
                    text = (self.root / path).read_text(encoding="utf-8", errors="replace")
                    text = "[arquivo novo/estado atual]\n" + text
                except OSError:
                    text = ""
            item["diff"] = text[:20000]
            changes.append(item)
        return changes

