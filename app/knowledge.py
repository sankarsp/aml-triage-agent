"""Embedding providers and the pgvector-backed knowledge store."""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Protocol

import numpy as np

from .config import get_settings
from .db import connection


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class LocalHashEmbeddings:
    """Offline, deterministic feature-hashing embedder (unigrams + bigrams).

    Good enough to exercise the vector pipeline without AWS; switch to Bedrock
    (EMBEDDING_PROVIDER=bedrock) for real semantic retrieval.
    """

    def __init__(self, dim: int):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        feats = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        v = np.zeros(self.dim, dtype=np.float32)
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return (v / n).tolist() if n else v.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "local":
        return LocalHashEmbeddings(s.embedding_dim)
    from langchain_aws import BedrockEmbeddings

    return BedrockEmbeddings(
        model_id=s.bedrock_embedding_model_id,
        region_name=s.aws_region,
        model_kwargs={"dimensions": s.embedding_dim, "normalize": True},
    )


# --- Knowledge base (typologies, internal policy, regulation extracts) ----

def chunk_markdown(text: str, max_chars: int = 1800) -> list[tuple[str, str]]:
    """Split a markdown doc into (title, body) chunks on ##/### headings."""
    doc_title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "Untitled")
    parts = re.split(r"^(#{2,3} .+)$", text, flags=re.M)
    chunks: list[tuple[str, str]] = []
    intro = parts[0].strip()
    if intro and len(parts) == 1:
        chunks.append((doc_title, intro))
    for i in range(1, len(parts), 2):
        heading = parts[i].lstrip("#").strip()
        body = parts[i + 1].strip()
        if not body:
            continue
        for j in range(0, len(body), max_chars):
            suffix = f" ({j // max_chars + 1})" if len(body) > max_chars else ""
            chunks.append((f"{doc_title} — {heading}{suffix}", body[j : j + max_chars]))
    return chunks


def ingest_document(source: str, kind: str, text: str) -> int:
    chunks = chunk_markdown(text)
    if not chunks:
        return 0
    vectors = get_embedder().embed_documents([f"{t}\n{b}" for t, b in chunks])
    with connection() as conn:
        for (title, body), vec in zip(chunks, vectors):
            conn.execute(
                """INSERT INTO knowledge_chunks (source, kind, title, content, embedding)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (source, title)
                   DO UPDATE SET content = EXCLUDED.content, kind = EXCLUDED.kind,
                                 embedding = EXCLUDED.embedding""",
                (source, kind, title, body, np.array(vec, dtype=np.float32)),
            )
    return len(chunks)


def search_knowledge(query: str, k: int = 4) -> list[dict]:
    qv = np.array(get_embedder().embed_query(query), dtype=np.float32)
    with connection() as conn:
        rows = conn.execute(
            """SELECT source, kind, title, content, 1 - (embedding <=> %s) AS similarity
               FROM knowledge_chunks ORDER BY embedding <=> %s LIMIT %s""",
            (qv, qv, k),
        ).fetchall()
    for r in rows:
        r["similarity"] = round(float(r["similarity"]), 3)
    return rows


def similar_cases(query: str, k: int = 3, exclude_alert: str | None = None) -> list[dict]:
    """Past analyst-reviewed decisions most similar to this case (institutional memory)."""
    qv = np.array(get_embedder().embed_query(query), dtype=np.float32)
    with connection() as conn:
        rows = conn.execute(
            """SELECT alert_id, final_disposition, risk_score, summary, analyst_comment,
                      1 - (embedding <=> %s) AS similarity
               FROM case_decisions WHERE alert_id IS DISTINCT FROM %s
               ORDER BY embedding <=> %s LIMIT %s""",
            (qv, exclude_alert, qv, k),
        ).fetchall()
    for r in rows:
        r["similarity"] = round(float(r["similarity"]), 3)
    return rows


def embed_text(text: str) -> np.ndarray:
    return np.array(get_embedder().embed_query(text), dtype=np.float32)

