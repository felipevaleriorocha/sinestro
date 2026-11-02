"""Armazenamento vetorial local e busca semântica para RAG no Context Manager.

Suporta indexação de documentos, divisão em chunks com overlap,
geração de embeddings (vetorizador denso determinístico baseado em hash/n-gramas
sem dependências externas, ou plugável via provedores) e cálculo de similaridade
por cosseno com persistência SQLite.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("memory.vector_store")


@dataclass
class SearchResult:
    """Resultado de uma busca semântica no banco vetorial."""
    doc_id: str
    chunk_id: str
    content: str
    score: float
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "content": self.content,
            "score": round(self.score, 4),
            "metadata": self.metadata,
        }


class HashedDenseEmbedding:
    """Vetorizador denso baseado em hash de termos e n-gramas.

    Zero dependências externas, determinístico, thread-safe.
    Produz vetores unitários de dimensão D onde a similaridade por cosseno
    é exatamente o produto escalar (dot product).
    """

    def __init__(self, dimension: int = 128) -> None:
        self.dimension = dimension

    def _tokenize(self, text: str) -> list[str]:
        words = re.findall(r"\w+", text.lower())
        tokens: list[str] = list(words)
        # Adiciona n-gramas de caracteres para capturar semelhanças morfológicas e subpalavras
        for w in words:
            if len(w) >= 4:
                for i in range(len(w) - 2):
                    tokens.append(w[i : i + 3])
        return tokens

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            tokens = self._tokenize(text)
            if not tokens:
                embeddings.append(vec)
                continue
            for token in tokens:
                h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dimension
                sign = 1.0 if ((h // self.dimension) % 2 == 0) else -1.0
                vec[idx] += sign

            # Normalização L2 para garantir norma unitária (||v|| = 1.0)
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0.0:
                vec = [x / norm for x in vec]
            embeddings.append(vec)
        return embeddings


def chunk_text(text: str, chunk_size: int = 400, chunk_overlap: int = 50) -> list[str]:
    """Divide o texto em blocos de tamanho chunk_size com sobreposição chunk_overlap."""
    if not text or not text.strip():
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_len = 0

    for p in paragraphs:
        p_len = len(p)
        if current_len + p_len + 1 <= chunk_size:
            current_chunk.append(p)
            current_len += p_len + 1
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
            if p_len > chunk_size:
                start = 0
                while start < p_len:
                    end = min(start + chunk_size, p_len)
                    chunk_sub = p[start:end].strip()
                    if chunk_sub:
                        chunks.append(chunk_sub)
                    start += max(1, chunk_size - chunk_overlap)
                current_chunk = []
                current_len = 0
            else:
                current_chunk = [p]
                current_len = p_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


class VectorStore:
    """Banco vetorial local com persistência SQLite e busca por similaridade de cosseno."""

    def __init__(
        self,
        db_path: Path | str = ":memory:",
        embedding_fn: Callable[[list[str]], list[list[float]]] | None = None,
        dimension: int = 128,
    ) -> None:
        self.db_path = str(db_path)
        self.dimension = dimension
        self._embedder = embedding_fn or HashedDenseEmbedding(dimension=dimension).embed
        self._mem_con: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._mem_con = sqlite3.connect(":memory:")
            self._mem_con.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._mem_con is not None:
            return self._mem_con
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    def _init_db(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    embedding BLOB NOT NULL,
                    FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE
                );
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_collection ON chunks(collection);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);")

    def add_document(
        self,
        doc_id: str,
        content: str,
        metadata: dict | None = None,
        collection: str = "default",
        chunk_size: int = 400,
        chunk_overlap: int = 50,
    ) -> list[str]:
        """Indexa um documento, divide em chunks, gera embeddings e persiste."""
        metadata = metadata or {}
        chunks = chunk_text(content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if not chunks:
            chunks = [content]

        embeddings = self._embedder(chunks)
        chunk_ids: list[str] = []

        with self._get_connection() as con:
            # Substitui documento anterior com mesmo id se existir
            con.execute(
                "INSERT OR REPLACE INTO documents(id, collection, content, metadata_json) VALUES(?, ?, ?, ?);",
                (doc_id, collection, content, json.dumps(metadata, ensure_ascii=False)),
            )
            con.execute("DELETE FROM chunks WHERE doc_id=?;", (doc_id,))

            for idx, (chunk_str, emb) in enumerate(zip(chunks, embeddings)):
                cid = f"{doc_id}#chunk_{idx}"
                chunk_ids.append(cid)
                chunk_meta = {**metadata, "chunk_index": idx, "doc_id": doc_id}
                blob = struct.pack(f"{len(emb)}f", *emb)
                con.execute(
                    """
                    INSERT INTO chunks(id, doc_id, collection, chunk_index, content, metadata_json, embedding)
                    VALUES(?, ?, ?, ?, ?, ?, ?);
                    """,
                    (cid, doc_id, collection, idx, chunk_str, json.dumps(chunk_meta, ensure_ascii=False), blob),
                )
        return chunk_ids

    def search(
        self,
        query: str,
        top_k: int = 5,
        collection: str = "default",
        min_score: float = 0.05,
        filter_metadata: dict | None = None,
    ) -> list[SearchResult]:
        """Realiza busca vetorial por similaridade de cosseno retornando os top_k chunks mais relevantes."""
        query_text = (query or "").strip()
        if not query_text:
            return []

        q_emb = self._embedder([query_text])[0]
        q_norm = math.sqrt(sum(x * x for x in q_emb))
        if q_norm == 0.0:
            return []

        results: list[SearchResult] = []
        with self._get_connection() as con:
            cursor = con.execute(
                "SELECT id, doc_id, content, metadata_json, embedding FROM chunks WHERE collection=?;",
                (collection,),
            )
            for row in cursor.fetchall():
                meta = json.loads(row["metadata_json"] or "{}")
                if filter_metadata:
                    match = all(meta.get(k) == v for k, v in filter_metadata.items())
                    if not match:
                        continue

                blob = row["embedding"]
                dim = len(blob) // 4
                c_emb = struct.unpack(f"{dim}f", blob)

                # Produto escalar (já unit-normalizado)
                dot = sum(q * c for q, c in zip(q_emb, c_emb))
                score = max(0.0, dot)
                if score >= min_score:
                    results.append(
                        SearchResult(
                            doc_id=row["doc_id"],
                            chunk_id=row["id"],
                            content=row["content"],
                            score=score,
                            metadata=meta,
                        )
                    )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def delete_document(self, doc_id: str, collection: str = "default") -> None:
        """Remove um documento e seus chunks associados."""
        with self._get_connection() as con:
            con.execute("DELETE FROM documents WHERE id=? AND collection=?;", (doc_id, collection))
            con.execute("DELETE FROM chunks WHERE doc_id=? AND collection=?;", (doc_id, collection))

    def count(self, collection: str = "default") -> int:
        """Retorna a contagem de chunks indexados na coleção."""
        with self._get_connection() as con:
            row = con.execute("SELECT COUNT(*) AS cnt FROM chunks WHERE collection=?;", (collection,)).fetchone()
            return int(row["cnt"]) if row else 0

    def clear(self, collection: str | None = None) -> None:
        """Limpa coleção ou todo o banco vetorial."""
        with self._get_connection() as con:
            if collection:
                con.execute("DELETE FROM documents WHERE collection=?;", (collection,))
                con.execute("DELETE FROM chunks WHERE collection=?;", (collection,))
            else:
                con.execute("DELETE FROM documents;")
                con.execute("DELETE FROM chunks;")

    def close(self) -> None:
        """Fecha eventuais recursos associados."""
        if self._mem_con is not None:
            self._mem_con.close()
            self._mem_con = None
