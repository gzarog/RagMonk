"""Search Quality Improvement Plan, Phase 12: the new/extended
``files_repo`` primitives ``decide_reprocessing``-driven reprocessing
depends on -- ``mark_indexed``'s ``parser_version``/``chunker_version``
stamping, ``update_embedding_version``, and ``rename``.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import FileKind, FileRecord, FileStatus
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import files_repo
from ragmonk.storage.sqlite import connect


def _seed(conn) -> None:  # noqa: ANN001
    files_repo.insert(
        conn,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/root/a.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=1.0,
            content_hash="abc",
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )


def test_mark_indexed_stamps_parser_and_chunker_version_when_given(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)

        files_repo.mark_indexed(
            conn,
            "f1",
            size=10,
            mtime=1.0,
            content_hash="abc",
            status=FileStatus.INDEXED,
            indexed_at="t1",
            parser_version="p1",
            chunker_version="c1",
        )

        record = files_repo.get(conn, "f1")
        assert record is not None
        assert record.parser_version == "p1"
        assert record.chunker_version == "c1"
        assert record.generation == 1
    finally:
        conn.close()


def test_mark_indexed_leaves_version_stamps_untouched_when_not_given(tmp_path: Path) -> None:
    """A processor with no version provider (e.g. ``raw_processor``, or
    today's ``code_processor``) passes neither -- the previously stamped
    value (if any) must survive, never get clobbered to ``NULL``.
    """
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)
        files_repo.mark_indexed(
            conn,
            "f1",
            size=10,
            mtime=1.0,
            content_hash="abc",
            status=FileStatus.INDEXED,
            indexed_at="t1",
            parser_version="p1",
            chunker_version="c1",
        )

        files_repo.mark_indexed(
            conn,
            "f1",
            size=11,
            mtime=2.0,
            content_hash="def",
            status=FileStatus.INDEXED,
            indexed_at="t2",
        )

        record = files_repo.get(conn, "f1")
        assert record is not None
        assert record.parser_version == "p1"
        assert record.chunker_version == "c1"
        assert record.content_hash == "def"
    finally:
        conn.close()


def test_update_embedding_version_stamps_model_and_text_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)

        files_repo.update_embedding_version(
            conn, "f1", embedding_model_id="model-a", embedding_text_version="1", updated_at="t1"
        )

        record = files_repo.get(conn, "f1")
        assert record is not None
        assert record.embedding_model_id == "model-a"
        assert record.embedding_text_version == "1"
    finally:
        conn.close()


def test_rename_keeps_id_and_content_hash_but_moves_path(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        _seed(conn)

        files_repo.rename(
            conn, "f1", new_path="/root/renamed.md", size=99, mtime=2.0, updated_at="t2"
        )

        record = files_repo.get(conn, "f1")
        assert record is not None
        assert record.path == "/root/renamed.md"
        assert record.size == 99
        assert record.mtime == 2.0
        assert record.content_hash == "abc"

        by_new_path = files_repo.get_by_path(conn, "s1", "/root/renamed.md")
        assert by_new_path is not None and by_new_path.id == "f1"
        assert files_repo.get_by_path(conn, "s1", "/root/a.md") is None

        # path_fts is kept in sync, not left pointing at the old path.
        hits = files_repo.search_path_projection(conn, "renamed")
        assert [h.id for h in hits] == ["f1"]
        assert files_repo.search_path_projection(conn, "a.md") == []
    finally:
        conn.close()


def test_list_missing_embeddings_finds_indexed_files_with_no_current_model_vectors(
    tmp_path: Path,
) -> None:
    """Indexing optimization plan, Phase P5: the ``ragmonk vectors
    backfill`` target -- an INDEXED CODE/DOCUMENT file whose
    ``embedding_model_id`` is ``NULL`` (never embedded, e.g. indexed
    while ``search.semantic`` was off) or stamped for a different model.
    Excludes: a file already current for this model, a file that hasn't
    finished indexing yet, and a non-CODE/DOCUMENT kind.
    """
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")

        def _file(file_id: str, *, kind: FileKind, status: FileStatus, model: str | None) -> None:
            files_repo.insert(
                conn,
                FileRecord(
                    id=file_id,
                    source_id="s1",
                    path=f"/root/{file_id}",
                    kind=kind,
                    size=1,
                    mtime=1.0,
                    status=status,
                    embedding_model_id=model,
                    created_at="now",
                    updated_at="now",
                ),
            )

        _file("never_embedded", kind=FileKind.CODE, status=FileStatus.INDEXED, model=None)
        _file("old_model", kind=FileKind.DOCUMENT, status=FileStatus.INDEXED, model="old-model")
        _file("current", kind=FileKind.CODE, status=FileStatus.INDEXED, model="new-model")
        _file("not_indexed_yet", kind=FileKind.CODE, status=FileStatus.QUEUED, model=None)
        _file("unknown_kind", kind=FileKind.UNKNOWN, status=FileStatus.INDEXED, model=None)

        missing = files_repo.list_missing_embeddings(conn, "s1", model_id="new-model")
        assert {f.id for f in missing} == {"never_embedded", "old_model"}
    finally:
        conn.close()
