"""Camada de contexto compartilhado (somente leitura do workspace do jogo).

Ambos os agentes recebem exatamente o mesmo pacote de contexto por workflow
(congelado no início do workflow e identificado por hash), mais as decisões
técnicas registradas. Categorias não encontradas são reportadas como ausentes
para que nenhum agente invente o conteúdo.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from memory.decision_store import DecisionStore


@dataclass
class ContextBundle:
    text: str
    sources: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    hash: str = ""

    def to_dict(self, include_text: bool = False) -> dict:
        d = {"hash": self.hash, "sources": self.sources, "missing": self.missing, "chars": len(self.text)}
        if include_text:
            d["text"] = self.text
        return d


class ContextManager:
    def __init__(self, project_root: Path, sources_file: Path, decisions: DecisionStore,
                 max_chars: int = 40000, file_max_chars: int = 12000):
        self.project_root = Path(project_root)
        self.sources_file = Path(sources_file)
        self.decisions = decisions
        self.max_chars = max_chars
        self.file_max_chars = file_max_chars
        self._frozen: dict[str, ContextBundle] = {}

    def _config(self) -> dict:
        try:
            return json.loads(self.sources_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"categories": [{"id": "agents", "label": "AGENTS.md", "patterns": ["AGENTS.md"], "required": True},
                                   {"id": "readme", "label": "README.md", "patterns": ["README.md"], "required": True}],
                    "exclude_dirs": [".git", "node_modules"], "max_files_per_category": 4}

    def _walk(self, exclude: set[str]) -> list[str]:
        files: list[str] = []
        root = self.project_root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in exclude and not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, root)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
            if depth > 6:
                dirnames[:] = []
            for name in filenames:
                if name.lower().endswith((".md", ".txt")):
                    rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                    files.append(rel.replace(os.sep, "/"))
            if len(files) > 5000:
                break
        return sorted(files)

    @staticmethod
    def _match(rel: str, pattern: str) -> bool:
        if "/" not in pattern:
            return rel == pattern
        if pattern.startswith("**/"):
            tail = pattern[3:]
            name = rel.rsplit("/", 1)[-1]
            return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel, tail) or fnmatch.fnmatchcase(name, tail)
        return fnmatch.fnmatchcase(rel, pattern)

    def discover(self) -> tuple[list[dict], list[str]]:
        cfg = self._config()
        if not self.project_root.is_dir():
            return [], [c.get("label", c["id"]) for c in cfg.get("categories", [])]
        all_files = self._walk(set(cfg.get("exclude_dirs", [])))
        per_cat = int(cfg.get("max_files_per_category", 4))
        used: set[str] = set()
        found: list[dict] = []
        missing: list[str] = []
        for cat in cfg.get("categories", []):
            matches = [f for f in all_files if f not in used and any(self._match(f, p) for p in cat.get("patterns", []))]
            matches = matches[:per_cat]
            if not matches and cat.get("required", True):
                missing.append(cat.get("label", cat["id"]))
            for m in matches:
                used.add(m)
                found.append({"category": cat["id"], "label": cat.get("label", cat["id"]), "path": m})
        return found, missing

    def build(self) -> ContextBundle:
        sources, missing = self.discover()
        pieces: list[str] = []
        total = 0
        out_sources = []
        for s in sources:
            path = self.project_root / s["path"]
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            content = raw[: self.file_max_chars]
            remaining = self.max_chars - total
            if remaining <= 500:
                out_sources.append({**s, "included": False, "reason": "limite total de contexto"})
                continue
            content = content[:remaining]
            truncated = len(content) < len(raw)
            pieces.append(f"--- [{s['label']}] {s['path']}{' (truncado)' if truncated else ''} ---\n{content}")
            total += len(content)
            out_sources.append({**s, "included": True, "chars": len(content), "truncated": truncated,
                                "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]})
        if missing:
            pieces.append("--- DOCUMENTOS NÃO ENCONTRADOS (não invente seu conteúdo) ---\n" + "\n".join(f"- {m}" for m in missing))
        pieces.append("--- DECISÕES TÉCNICAS REGISTRADAS NO ORQUESTRADOR ---\n" + self.decisions.as_context())
        text = "\n\n".join(pieces)
        return ContextBundle(text=text, sources=out_sources, missing=missing, hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])

    # Congelamento por workflow: ambos os agentes recebem o mesmo pacote.
    def for_workflow(self, workflow_id: str, refresh: bool = False) -> ContextBundle:
        if refresh or workflow_id not in self._frozen:
            self._frozen[workflow_id] = self.build()
        return self._frozen[workflow_id]

    def refresh_decisions(self, workflow_id: str) -> ContextBundle:
        """Reconstrói mantendo os mesmos documentos, para incluir decisões novas registradas durante o workflow."""
        return self.for_workflow(workflow_id, refresh=True)

    def forget(self, workflow_id: str) -> None:
        self._frozen.pop(workflow_id, None)
