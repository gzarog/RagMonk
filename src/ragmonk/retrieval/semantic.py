"""Real semantic/vector search (Phase 9), completing the seam
``planner.py`` has called into since Phase 5.

Embeds the query with the same model used to embed indexed content
(``retrieval/embedder.py``), scores it against the project's persistent
ANN index (``retrieval/ann.py``, USearch HNSW by default), and returns
ranked hits in a shape deliberately close to
``retrieval/lexical.py``'s ``SearchResult`` --
``kind``/``id``/``title``/``path``/``source_id``/``snippet``/``location``
line up field-for-field so a caller can render or evidence-ify a
``SemanticHit`` the same way it already does a lexical hit, plus one
extra ``score`` field lexical results have no equivalent of. It stays a
distinct type rather than literally reusing ``SearchResult``: a
similarity score is not a ``RankTier``, and blueprint section 57/19 is
explicit that semantic similarity must never be folded into or upgrade a
lexical/graph confidence tier -- keeping it a separate type is what makes
"never silently mixed in" structurally true rather than a convention
callers have to remember.

Never crashes ``explore``/``search`` if semantic search is unavailable
for any reason (disabled, embedding model can't load, no embeddings
computed yet) -- every failure path returns
``SemanticSearchResult(available=False, reason=...)`` instead of raising,
per the blueprint's AI-optional principle Phase 5 already established for
this exact seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ragmonk.code.graph import all_project_connections
from ragmonk.core import paths
from ragmonk.core.config import SearchConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import ann, embedder
from ragmonk.retrieval import cache as search_cache
from ragmonk.storage.repositories import embeddings_repo, vector_items_repo

DEFAULT_LIMIT = 15


@dataclass(frozen=True)
class SemanticHit:
    kind: str  # "entity" | "document"
    id: str
    title: str
    path: str
    source_id: str
    score: float
    snippet: str = ""
    location: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tier": "semantic",
            "id": self.id,
            "title": self.title,
            "path": self.path,
            "source_id": self.source_id,
            "snippet": self.snippet,
            "location": self.location,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True)
class SemanticSearchResult:
    available: bool
    reason: str
    results: tuple[SemanticHit, ...] = field(default_factory=tuple)


def _search_source_via_ann(
    conn: Any,
    *,
    source_id: str,
    project_id: str,
    home: Path,
    query_vector: list[float],
    k: int,
    model_id: str,
    engine: str,
) -> list[SemanticHit] | None:
    """One source's ANN-backed candidates plus their batched metadata
    lookup (blueprint sections 12/47), or ``None`` when this source has
    no embeddings for ``model_id`` at all -- the signal ``semantic_search``
    uses to skip this source entirely. Indexing always writes
    ``vector_items`` in the same pass as ``embeddings`` (see
    ``indexing/runner.py``), so there is no supported state where
    embeddings exist without a corresponding ANN entry to search.

    The brute-force backend has no persistence of its own (see
    ``retrieval/ann.py``'s ``BruteForceAnnIndex`` docstring), so it is
    seeded here from ``vector_items`` directly -- still one batched read,
    not the one-candidate-at-a-time cost the pre-ANN brute-force path had.
    """
    dim = embeddings_repo.get_dim_for_model(conn, model_id)
    if dim is None:
        return None

    index_path = paths.project_vector_index_path(project_id, home)
    meta_path = paths.project_vector_meta_path(project_id, home)
    if engine != "bruteforce" and not ann.is_index_compatible(
        meta_path, model_id=model_id, ndim=dim
    ):
        # Either no index has ever been built for this source, or it was
        # built for a since-changed model/dimensionality -- never load a
        # mismatched on-disk index (blueprint section 48), fall through
        # to the brute-force-over-vector_items path below instead.
        engine = "bruteforce"

    index, backend = ann.get_cached_backend(engine, ndim=dim, index_path=index_path)
    if backend == "bruteforce" and len(index) == 0:
        all_vectors = vector_items_repo.list_all_with_vectors(conn, model_id=model_id)
        if not all_vectors:
            return None
        index.add([vid for vid, _ in all_vectors], [vec for _, vec in all_vectors])
    if len(index) == 0:
        return None

    raw_hits = index.search(query_vector, k)
    if not raw_hits:
        return []
    metadata = vector_items_repo.batch_metadata_lookup(
        conn, [vector_id for vector_id, _ in raw_hits]
    )
    hits: list[SemanticHit] = []
    for vector_id, score in raw_hits:
        row = metadata.get(vector_id)
        if row is None:
            continue
        hits.append(
            SemanticHit(
                kind=row.kind,
                id=row.id,
                title=row.title,
                path=row.path,
                source_id=source_id,
                score=score,
                snippet=row.snippet,
                location=row.location,
            )
        )
    return hits


def _embed_query_cached(query: str, *, config: SearchConfig) -> list[float] | None:
    """A cached query embedding (blueprint section 24), or ``None`` on a
    cache miss/disabled cache -- the caller falls back to
    ``embedder.embed_texts`` and stores the result itself, since only it
    knows whether that call actually succeeded.
    """
    if not config.cache.enabled:
        return None
    embedding_cache = search_cache.get_query_embedding_cache(config.cache.max_query_embeddings)
    return embedding_cache.get(
        search_cache.embedding_cache_key(query=query, model_id=embedder.EMBEDDING_MODEL_ID)
    )


def _semantic_search_server(
    ctx: AppContext, query_vector: list[float], *, limit: int, k: int
) -> SemanticSearchResult:
    """Server-mode counterpart of the local ANN loop below: routes through
    ``ctx.backend().semantic_search`` (a real OpenSearch/Elasticsearch kNN
    call, P4/P5) instead of USearch/vector_items -- never local SQLite, per
    this phase's "no silent local fallback" rule.
    """
    raw_hits = ctx.backend().semantic_search(query_vector, k)
    if not raw_hits:
        return SemanticSearchResult(
            available=True, reason="no embeddings computed for this project yet", results=()
        )
    hits = [
        SemanticHit(
            kind="entity" if hit.kind == "entity" else "document",
            id=hit.id,
            title=str(
                hit.payload.get("qualified_name") or hit.payload.get("name") or hit.id
            ),
            path=str(hit.payload.get("path", "")),
            source_id=str(hit.payload.get("source_id", "")),
            score=hit.score,
            snippet=str(hit.payload.get("snippet") or ""),
            location=None,
        )
        for hit in raw_hits
    ]
    hits.sort(key=lambda h: (-h.score, h.path, h.id))
    return SemanticSearchResult(available=True, reason="ok", results=tuple(hits[:limit]))


def semantic_search(
    ctx: AppContext,
    query: str,
    *,
    config: SearchConfig,
    limit: int = DEFAULT_LIMIT,
    candidate_k: int | None = None,
) -> SemanticSearchResult:
    """Returns a skipped/unavailable result rather than raising whenever
    semantic search cannot actually run right now -- see the module
    docstring. Only ever called with ``config.semantic`` true by
    ``explore``/``search`` (both check first so the default, disabled
    path never even imports ``torch``/``transformers`` -- see
    ``retrieval/embedder.py``), but re-checked here too since this is the
    one function that could otherwise silently do real ML work.

    ``candidate_k`` (blueprint section 20's candidate budget) sizes the
    internal nearest-neighbor search independently of ``limit``, the
    final number of results returned -- defaults to ``limit`` itself when
    omitted, i.e. today's behavior of "fetch and return the same count".
    """
    query = query.strip()
    if not config.semantic:
        return SemanticSearchResult(available=False, reason="search.semantic is disabled")
    if not query:
        return SemanticSearchResult(available=True, reason="empty query", results=())

    query_vector = _embed_query_cached(query, config=config)
    if query_vector is None:
        try:
            query_vector = embedder.embed_texts([query])[0]
        except embedder.EmbeddingModelUnavailableError as exc:
            return SemanticSearchResult(
                available=False, reason=f"embedding model unavailable: {exc}", results=()
            )
        if config.cache.enabled:
            embedding_cache = search_cache.get_query_embedding_cache(
                config.cache.max_query_embeddings
            )
            embedding_cache.set(
                search_cache.embedding_cache_key(query=query, model_id=embedder.EMBEDDING_MODEL_ID),
                query_vector,
            )

    k = candidate_k if candidate_k is not None else limit
    model_id = embedder.EMBEDDING_MODEL_ID

    if ctx.config.storage.mode == "server":
        return _semantic_search_server(ctx, query_vector, limit=limit, k=k)

    hits: list[SemanticHit] = []
    for source_id, source_path, conn in all_project_connections(ctx):
        project_id = paths.project_id_for_path(Path(source_path))
        ann_hits = _search_source_via_ann(
            conn,
            source_id=source_id,
            project_id=project_id,
            home=ctx.home,
            query_vector=query_vector,
            k=k,
            model_id=model_id,
            engine=config.vector.engine,
        )
        if ann_hits is None:
            # No embeddings for this model in this source at all -- see
            # ``_search_source_via_ann``'s docstring for why that is the
            # only reason it returns ``None`` (never "no ANN index yet
            # for otherwise-present embeddings").
            continue
        hits.extend(ann_hits)

    if not hits:
        return SemanticSearchResult(
            available=True, reason="no embeddings computed for this project yet", results=()
        )

    hits.sort(key=lambda h: (-h.score, h.path, h.id))
    return SemanticSearchResult(available=True, reason="ok", results=tuple(hits[:limit]))
