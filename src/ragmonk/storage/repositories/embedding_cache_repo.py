"""CRUD for ``embedding_cache`` in a project's ``knowledge.db``.

Indexing optimization plan V2, Phase P3: a persistent cross-run cache of
already-computed embedding vectors, so re-indexing (or backfilling) text
this project has embedded before -- under the exact same model,
preprocessing and text-assembly identity -- never re-runs model
inference for it. See ``storage/schema.py``'s ``KNOWLEDGE_DB_V15`` for
the full key-design and retention-policy rationale (project isolation is
free -- this table lives in the same per-project database every other
project-scoped table does -- and it is deliberately never actively
garbage-collected, mirroring ``document_conversion_cache_repo``'s own
precedent).

``get``/``put`` take the composite key's four axes as explicit keyword
arguments, deliberately never a partial subset -- there is no fuzzy or
best-effort lookup here: a caller either supplies the exact text hash,
model id, preprocessing version and embedding-text version a row was
written under, or gets a miss.
"""

from __future__ import annotations

import hashlib
import sqlite3

from ragmonk.storage.repositories.embeddings_repo import pack_vector, unpack_vector


def text_hash(text: str) -> str:
    """A compact, deterministic key for ``text`` -- the exact string
    that was (or would be) handed to ``retrieval/embedder.embed_texts``,
    never a normalized/lowercased/whitespace-collapsed variant: this
    cache's whole safety property depends on the key describing *exactly*
    what was embedded, not an approximation of it.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(
    conn: sqlite3.Connection,
    hashed_text: str,
    *,
    model_id: str,
    preprocessing_version: str,
    embedding_text_version: str,
) -> list[float] | None:
    row = conn.execute(
        "SELECT vector FROM embedding_cache "
        "WHERE text_hash = ? AND model_id = ? AND preprocessing_version = ? "
        "AND embedding_text_version = ?",
        (hashed_text, model_id, preprocessing_version, embedding_text_version),
    ).fetchone()
    if row is None:
        return None
    return unpack_vector(row["vector"])


def put(
    conn: sqlite3.Connection,
    hashed_text: str,
    *,
    model_id: str,
    preprocessing_version: str,
    embedding_text_version: str,
    vector: list[float],
    created_at: str,
) -> None:
    """Upserts the cache row for this exact key. Always run inside the
    caller's own ``with transaction(conn):`` block (mirroring every other
    repo write in this project, e.g.
    ``document_conversion_cache_repo.put``) -- this function never commits
    itself, and ``indexing/embedding_indexer.publish_embeddings`` writes
    these rows in the very same transaction as the ``embeddings``/
    ``vector_items`` rows they were derived alongside, so an interrupted
    publish rolls both back together rather than leaving an orphaned
    cache entry for content that was never actually published.
    """
    conn.execute(
        """
        INSERT INTO embedding_cache (
            text_hash, model_id, preprocessing_version, embedding_text_version,
            vector, dim, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (text_hash, model_id, preprocessing_version, embedding_text_version)
        DO UPDATE SET vector = excluded.vector, dim = excluded.dim, created_at = excluded.created_at
        """,
        (
            hashed_text,
            model_id,
            preprocessing_version,
            embedding_text_version,
            pack_vector(vector),
            len(vector),
            created_at,
        ),
    )


def count_all(conn: sqlite3.Connection) -> int:
    """Total cached-vector row count -- diagnostics/telemetry only."""
    row = conn.execute("SELECT COUNT(*) AS c FROM embedding_cache").fetchone()
    return int(row["c"])
