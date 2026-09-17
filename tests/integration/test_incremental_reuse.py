"""Search Quality Improvement Plan, Phase 12, end-to-end through the real
CLI: derived processing (chunks, FTS, embeddings) reused or rebuilt
exactly per the plan's rules, proven not just by "a rebuild was
triggered" but by what actually ends up stored/searchable afterwards --
the acceptance gate is "no stale data ever silently served".

The real embedding model is never loaded here, mirroring
``test_semantic_retrieval.py``: ``retrieval/embedder.embed_texts`` is
monkeypatched to a small deterministic function.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.documents import chunker as chunker_module
from ragmonk.documents.chunker import Chunk
from ragmonk.retrieval import embedder
from ragmonk.storage.repositories import documents_repo, embeddings_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _fake_embed_texts(texts: list[str]) -> list[list[float]]:
    return [[float(len(t)), float(sum(map(ord, t)) % 997)] for t in texts]


def _fake_embed_texts_v2(texts: list[str]) -> list[list[float]]:
    """A deliberately different transform standing in for "a different
    embedding model would produce different vectors for the same text" --
    ``_fake_embed_texts`` alone can't prove that, since it's a pure
    function of the text and the text never changes in the
    ``embedding_model_id``-bump scenario.
    """
    return [[float(len(t)) + 1000.0, float(sum(map(ord, t)) % 997) + 1000.0] for t in texts]


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedder, "embed_texts", _fake_embed_texts)


def _knowledge_conn(home: Path, source_path: Path):  # noqa: ANN201 - test helper
    project_id = paths.project_id_for_path(source_path)
    conn = connect(paths.project_db_path(project_id, home))
    return conn


def _sections(conn, file_id: str) -> list[tuple[str, int, str]]:  # noqa: ANN001
    rows = conn.execute(
        "SELECT id, generation, text FROM document_sections WHERE file_id = ? ORDER BY order_index",
        (file_id,),
    ).fetchall()
    return [(r["id"], r["generation"], r["text"]) for r in rows]


def _file_id(conn, path: str) -> str:  # noqa: ANN001
    row = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
    assert row is not None
    return row["id"]


def _init_and_add_source(
    runner: CliRunner, tmp_path: Path, root: Path
) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0


def test_path_only_change_reuses_chunks_and_embeddings(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(FIXTURES / "simple.md", root / "simple.md")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    _init_and_add_source(runner, tmp_path, root)
    first = runner.invoke(app, ["index"])
    assert first.exit_code == 0, first.output
    assert "embedded=0" not in first.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        file_id = _file_id(conn, str(root / "simple.md"))
        before_sections = _sections(conn, file_id)
        before_vectors = {
            row.subject_id: row.vector for row in embeddings_repo.list_by_model(
                conn, embedder.EMBEDDING_MODEL_ID
            )
            if row.file_id == file_id
        }
        assert before_sections
        assert before_vectors
    finally:
        conn.close()

    (root / "simple.md").rename(root / "renamed.md")

    second = runner.invoke(app, ["index"])
    assert second.exit_code == 0, second.output
    assert "moved=1" in second.output
    assert "new=0" in second.output
    assert "changed=0" in second.output
    assert "indexed=0" in second.output
    assert "embedded=0" in second.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        moved_file_id = _file_id(conn, str(root / "renamed.md"))
        assert moved_file_id == file_id
        after_sections = _sections(conn, file_id)
        assert after_sections == before_sections

        after_vectors = {
            row.subject_id: row.vector for row in embeddings_repo.list_by_model(
                conn, embedder.EMBEDDING_MODEL_ID
            )
            if row.file_id == file_id
        }
        assert after_vectors == before_vectors

        hits = documents_repo.search_fts(conn, "Section")
        assert hits
    finally:
        conn.close()


def test_chunker_version_bump_rebuilds_and_serves_the_new_derivation(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(FIXTURES / "simple.md", root / "simple.md")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    _init_and_add_source(runner, tmp_path, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        file_id = _file_id(conn, str(root / "simple.md"))
        before_sections = _sections(conn, file_id)
        assert any("Paragraph in section one." in text for _id, _gen, text in before_sections)
        assert documents_repo.search_fts(conn, "Paragraph in section one")
    finally:
        conn.close()

    def _fake_chunk_document(normalized, *, config=None, doc_title=""):  # noqa: ANN001, ANN201
        return [
            Chunk(
                kind="paragraph",
                text="SENTINEL REBUILT PARAGRAPH",
                heading_level=None,
                heading_path=(),
                parent_index=None,
                page_start=None,
                page_end=None,
                contextual_text="SENTINEL REBUILT PARAGRAPH",
                search_text="SENTINEL REBUILT PARAGRAPH",
                token_count=3,
            )
        ]

    monkeypatch.setattr(chunker_module, "CHUNKER_VERSION", "999-test")
    monkeypatch.setattr(chunker_module, "chunk_document", _fake_chunk_document)

    second = runner.invoke(app, ["index"])
    assert second.exit_code == 0, second.output
    assert "changed=1" in second.output
    assert "embedded=" in second.output
    assert "embedded=0" not in second.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        after_sections = _sections(conn, file_id)
        after_texts = {text for _id, _gen, text in after_sections}
        assert after_texts == {"SENTINEL REBUILT PARAGRAPH"}
        # The old ids are entirely gone (delete-then-insert), and the old
        # generation's ids never linger alongside the new ones.
        before_ids = {i for i, _gen, _text in before_sections}
        after_ids = {i for i, _gen, _text in after_sections}
        assert before_ids.isdisjoint(after_ids)

        # No stale data is silently served: the old text is no longer
        # findable at all, and the new text is.
        assert documents_repo.search_fts(conn, "Paragraph in section one") == []
        assert documents_repo.search_fts(conn, "SENTINEL REBUILT PARAGRAPH")

        new_vectors = [
            row for row in embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
            if row.file_id == file_id
        ]
        assert len(new_vectors) == 1
        assert new_vectors[0].subject_id in after_ids
    finally:
        conn.close()


def test_embedding_model_id_bump_rebuilds_only_vectors(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(FIXTURES / "simple.md", root / "simple.md")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    _init_and_add_source(runner, tmp_path, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    original_model_id = embedder.EMBEDDING_MODEL_ID
    conn = _knowledge_conn(ragmonk_home, root)
    try:
        file_id = _file_id(conn, str(root / "simple.md"))
        before_sections = _sections(conn, file_id)
        before_vectors = {
            row.subject_id: row.vector
            for row in embeddings_repo.list_by_model(conn, original_model_id)
            if row.file_id == file_id
        }
        assert before_vectors
    finally:
        conn.close()

    monkeypatch.setattr(embedder, "EMBEDDING_MODEL_ID", "fake-model-v2")
    monkeypatch.setattr(embedder, "embed_texts", _fake_embed_texts_v2)

    second = runner.invoke(app, ["index"])
    assert second.exit_code == 0, second.output
    # Content-wise nothing changed: no full reprocess.
    assert "new=0" in second.output
    assert "changed=0" in second.output
    assert "indexed=0" in second.output
    # But the embedding step did run and recompute vectors.
    assert "embedded=0" not in second.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        after_sections = _sections(conn, file_id)
        assert after_sections == before_sections  # chunks/FTS untouched

        old_model_rows = [
            row for row in embeddings_repo.list_by_model(conn, original_model_id)
            if row.file_id == file_id
        ]
        # embeddings_repo.delete_by_file wipes every model's rows for this
        # file -- the stale old-model vector is never left behind
        # alongside the new one.
        assert old_model_rows == []

        new_model_rows = {
            row.subject_id: row.vector
            for row in embeddings_repo.list_by_model(conn, "fake-model-v2")
            if row.file_id == file_id
        }
        assert new_model_rows.keys() == before_vectors.keys()
        for subject_id, old_vector in before_vectors.items():
            assert new_model_rows[subject_id] != old_vector
    finally:
        conn.close()


def test_no_version_change_and_unchanged_content_rebuilds_nothing(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(FIXTURES / "simple.md", root / "simple.md")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    _init_and_add_source(runner, tmp_path, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        file_id = _file_id(conn, str(root / "simple.md"))
        before_sections = _sections(conn, file_id)
        before_vectors = {
            row.subject_id: row.vector
            for row in embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
            if row.file_id == file_id
        }
        before_updated_at = conn.execute(
            "SELECT updated_at FROM files WHERE id = ?", (file_id,)
        ).fetchone()["updated_at"]
    finally:
        conn.close()

    second = runner.invoke(app, ["index"])
    assert second.exit_code == 0, second.output
    assert "new=0" in second.output
    assert "changed=0" in second.output
    assert "moved=0" in second.output
    assert "indexed=0" in second.output
    assert "embedded=0" in second.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        after_sections = _sections(conn, file_id)
        assert after_sections == before_sections
        after_vectors = {
            row.subject_id: row.vector
            for row in embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
            if row.file_id == file_id
        }
        assert after_vectors == before_vectors
        after_updated_at = conn.execute(
            "SELECT updated_at FROM files WHERE id = ?", (file_id,)
        ).fetchone()["updated_at"]
        assert after_updated_at == before_updated_at
    finally:
        conn.close()
