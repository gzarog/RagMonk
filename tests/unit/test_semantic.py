"""``retrieval/semantic.py``'s real implementation: never crashes while
disabled (Phase 5's default), degrades gracefully when the embedding
model is unavailable or nothing has been embedded yet, and ranks stored
embeddings correctly once they exist -- all using precomputed fake
vectors and a monkeypatched ``embedder.embed_texts``, never the real
model (see ``tests/unit/test_embedder.py`` / the ``embedding_model``
marker for that).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core import paths
from ragmonk.core.config import SearchConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import (
    EmbeddingSubjectType,
    Entity,
    EntityType,
    FileKind,
    FileRecord,
    FileStatus,
)
from ragmonk.retrieval import embedder
from ragmonk.retrieval.semantic import semantic_search
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import (
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragmonk.storage.sqlite import transaction


@pytest.fixture
def ctx(ragmonk_home: Path, tmp_path: Path) -> AppContext:
    context = AppContext.bootstrap(home=ragmonk_home, cwd=tmp_path)
    yield context
    context.close()


def _register_source(context: AppContext, path: Path) -> str:
    """Registers ``path`` as a source and returns its ``knowledge.db``
    project id (``core.paths.project_id_for_path``) -- the key
    ``semantic_search`` (via ``code/graph.py``'s ``all_project_connections``)
    actually opens the project connection under, distinct from the
    source's own registry id.
    """
    registry = SourceRegistry(context.sources_conn, home=context.home)
    registry.add(str(path))
    return paths.project_id_for_path(path)


def test_disabled_semantic_search_returns_a_skipped_result(ctx: AppContext) -> None:
    result = semantic_search(ctx, "anything", config=SearchConfig(semantic=False))
    assert result.available is False
    assert result.results == ()
    assert "disabled" in result.reason


def test_enabled_semantic_search_with_no_embeddings_is_available_but_empty(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _register_source(ctx, project_root)
    monkeypatch.setattr(embedder, "embed_texts", lambda texts: [[1.0, 0.0]] * len(texts))

    result = semantic_search(ctx, "anything", config=SearchConfig(semantic=True))
    assert result.available is True
    assert result.results == ()
    assert "no embeddings" in result.reason


def test_enabled_semantic_search_degrades_when_model_unavailable(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    _register_source(ctx, project_root)

    def _raise(texts: list[str]) -> list[list[float]]:
        raise embedder.EmbeddingModelUnavailableError("no network")

    monkeypatch.setattr(embedder, "embed_texts", _raise)

    result = semantic_search(ctx, "anything", config=SearchConfig(semantic=True))
    assert result.available is False
    assert result.results == ()
    assert "unavailable" in result.reason


def test_enabled_semantic_search_ranks_stored_embeddings_by_cosine_similarity(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    project_id = _register_source(ctx, project_root)

    conn = ctx.project_conn(project_id)
    apply_migrations(conn, "knowledge")
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id=project_id,
            path="pkg/dog.py",
            kind=FileKind.CODE,
            size=1,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    with transaction(conn):
        entities_repo.insert(
            conn,
            Entity(
                id="e_close",
                source_id=project_id,
                file_id="f1",
                kind=EntityType.FUNCTION,
                name="bark_loudly",
                qualified_name="pkg.Dog.bark_loudly",
                language="python",
                signature="def bark_loudly(self): ...",
                start_line=1,
                end_line=2,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def bark_loudly(self): ...",
        )
        entities_repo.insert(
            conn,
            Entity(
                id="e_far",
                source_id=project_id,
                file_id="f1",
                kind=EntityType.FUNCTION,
                name="unrelated",
                qualified_name="pkg.Dog.unrelated",
                language="python",
                signature="def unrelated(self): ...",
                start_line=3,
                end_line=4,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
            snippet="def unrelated(self): ...",
        )
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e_close",
            file_id="f1",
            source_id=project_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
            vector=[1.0, 0.0, 0.0],
        )
        vector_items_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e_close",
            file_id="f1",
            source_id=project_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
        )
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e_far",
            file_id="f1",
            source_id=project_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
            vector=[0.0, 1.0, 0.0],
        )
        vector_items_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e_far",
            file_id="f1",
            source_id=project_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
        )
        # A stale row tagged with a different (superseded) model id --
        # must never be scored against a query vector from the current
        # model's vector space, even though it is otherwise a perfect
        # match for the query below.
        embeddings_repo.insert(
            conn,
            subject_type=EmbeddingSubjectType.ENTITY,
            subject_id="e_close",
            file_id="f1",
            source_id=project_id,
            model_id="some-other-old-model",
            vector=[1.0, 0.0, 0.0],
        )

    monkeypatch.setattr(embedder, "embed_texts", lambda texts: [[1.0, 0.0, 0.0]] * len(texts))

    result = semantic_search(ctx, "bark loudly", config=SearchConfig(semantic=True), limit=5)
    assert result.available is True
    assert [h.id for h in result.results] == ["e_close", "e_far"]
    assert result.results[0].score == pytest.approx(1.0)
    assert result.results[0].kind == "entity"
    assert result.results[0].to_dict()["tier"] == "semantic"
    assert result.results[1].score == pytest.approx(0.0)
