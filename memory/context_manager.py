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
from memory.vector_store import SearchResult, VectorStore


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
    def __init__(
        self,
        project_root: Path,
        sources_file: Path | None = None,
        decisions: DecisionStore | None = None,
        max_chars: int = 40000,
        file_max_chars: int = 12000,
        sources_config: dict | None = None,
        vector_store: VectorStore | None = None,
    ):
        self.project_root = Path(project_root)
        self.sources_file = Path(sources_file) if sources_file else None
        self.decisions = decisions
        self.max_chars = max_chars
        self.file_max_chars = file_max_chars
        self._sources_config = sources_config
        self.vector_store = vector_store or VectorStore()
        self._frozen: dict[str, ContextBundle] = {}
        self._workflow_queries: dict[str, str] = {}
        self._indexed: bool = False

    def _config(self) -> dict:
        if self._sources_config is not None:
            return self._sources_config
        if self.sources_file and self.sources_file.is_file():
            try:
                return json.loads(self.sources_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
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
        if self.decisions:
            pieces.append("--- DECISÕES TÉCNICAS REGISTRADAS NO ORQUESTRADOR ---\n" + self.decisions.as_context())
        text = "\n\n".join(pieces)
        return ContextBundle(text=text, sources=out_sources, missing=missing, hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])

    # Congelamento por workflow: ambos os agentes recebem o mesmo pacote.
    def for_workflow(self, workflow_id: str, refresh: bool = False, query: str | None = None) -> ContextBundle:
        if query:
            self._workflow_queries[workflow_id] = query
        else:
            query = self._workflow_queries.get(workflow_id)

        if refresh or workflow_id not in self._frozen:
            self._frozen[workflow_id] = self.build_augmented(query) if query else self.build()
        return self._frozen[workflow_id]

    def refresh_decisions(self, workflow_id: str) -> ContextBundle:
        """Reconstrói mantendo os mesmos documentos, para incluir decisões novas registradas durante o workflow."""
        return self.for_workflow(workflow_id, refresh=True)

    def forget(self, workflow_id: str) -> None:
        self._frozen.pop(workflow_id, None)
        self._workflow_queries.pop(workflow_id, None)

    # ---------------------------------------------------------------- RAG & Busca Semântica
    def index_workspace(self, refresh: bool = False) -> int:
        """Indexa arquivos de documentação do workspace e decisões técnicas no banco vetorial."""
        if self._indexed and not refresh:
            return self.vector_store.count()

        if refresh:
            self.vector_store.clear()

        sources, _ = self.discover()
        indexed_count = 0
        for s in sources:
            path = self.project_root / s["path"]
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            chunks = self.vector_store.add_document(
                doc_id=s["path"],
                content=content,
                metadata={"path": s["path"], "category": s["category"], "label": s.get("label", s["path"]), "type": "doc"},
            )
            indexed_count += len(chunks)

        if self.decisions:
            for d in self.decisions.list(limit=200):
                text = (
                    f"Decisão #{d['id']}: {d['title']}\n"
                    f"Status: {d.get('status')}\n"
                    f"Decisão: {d.get('decision')}\n"
                    f"Justificativa: {d.get('rationale', '')}\n"
                    f"Tags: {', '.join(d.get('tags', []))}"
                )
                chunks = self.vector_store.add_document(
                    doc_id=f"decision_{d['id']}",
                    content=text,
                    metadata={"decision_id": d["id"], "title": d["title"], "type": "decision"},
                )
                indexed_count += len(chunks)

        self._indexed = True
        return indexed_count

    def search_relevant(self, query: str, top_k: int = 5, min_score: float = 0.05) -> list[SearchResult]:
        """Recupera os blocos de contexto e decisões mais relevantes para a query."""
        if not self._indexed:
            self.index_workspace()
        return self.vector_store.search(query, top_k=top_k, min_score=min_score)

    def build_augmented(self, query: str | None = None, top_k: int = 5) -> ContextBundle:
        """Gera pacote de contexto enriquecido com RAG semântico para a query informada."""
        base_bundle = self.build()
        if not query or not query.strip():
            return base_bundle

        results = self.search_relevant(query, top_k=top_k)
        if not results:
            return base_bundle

        rag_pieces: list[str] = [
            f"--- RELEVÂNCIA SEMÂNTICA (RAG) PARA O PEDIDO: '{query.strip()[:120]}' ---"
        ]
        for idx, r in enumerate(results, 1):
            source_tag = r.metadata.get("path") or f"Decisão #{r.metadata.get('decision_id', '')} ({r.metadata.get('title', '')})"
            rag_pieces.append(f"[{idx}] {source_tag} (score: {round(r.score, 3)}):\n{r.content.strip()}")

        combined_text = "\n\n".join(rag_pieces) + "\n\n" + base_bundle.text
        bundle_hash = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()[:16]
        return ContextBundle(
            text=combined_text,
            sources=base_bundle.sources,
            missing=base_bundle.missing,
            hash=bundle_hash,
        )
