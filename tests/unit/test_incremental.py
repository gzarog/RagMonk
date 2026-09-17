from __future__ import annotations

from ragpilot.core.models import FileKind, FileRecord, FileStatus, ScannedFile
from ragpilot.indexing.incremental import (
    ChangeType,
    ReprocessDecision,
    VersionStamp,
    classify_change,
    decide_reprocessing,
    find_deleted,
)


def _record(**overrides: object) -> FileRecord:
    defaults: dict[str, object] = dict(
        id="f1",
        source_id="s1",
        path="/root/a.py",
        kind=FileKind.CODE,
        size=10,
        mtime=100.0,
        content_hash="abc",
        status=FileStatus.INDEXED,
        created_at="now",
        updated_at="now",
    )
    defaults.update(overrides)
    return FileRecord(**defaults)  # type: ignore[arg-type]


def test_new_file_is_classified_new() -> None:
    change, digest = classify_change(None, 10, 100.0, lambda: "hash1")
    assert change is ChangeType.NEW
    assert digest == "hash1"


def test_unchanged_stat_skips_hashing() -> None:
    calls = []

    def compute() -> str:
        calls.append(1)
        return "should-not-be-called"

    prev = _record(size=10, mtime=100.0, content_hash="abc")
    change, digest = classify_change(prev, 10, 100.0, compute)
    assert change is ChangeType.UNCHANGED
    assert digest == "abc"
    assert calls == []


def test_changed_stat_with_different_hash_is_changed() -> None:
    prev = _record(size=10, mtime=100.0, content_hash="abc")
    change, digest = classify_change(prev, 11, 200.0, lambda: "xyz")
    assert change is ChangeType.CHANGED
    assert digest == "xyz"


def test_changed_stat_but_same_hash_is_unchanged() -> None:
    """A touch that bumps mtime without changing content must not re-trigger
    indexing once the content hash is recomputed and found identical."""
    prev = _record(size=10, mtime=100.0, content_hash="abc")
    change, digest = classify_change(prev, 10, 999.0, lambda: "abc")
    assert change is ChangeType.UNCHANGED
    assert digest == "abc"


def test_find_deleted_returns_records_missing_from_scan() -> None:
    prev = _record(path="/root/gone.py")
    kept = _record(id="f2", path="/root/kept.py")
    existing = {"/root/gone.py": prev, "/root/kept.py": kept}
    scanned = [ScannedFile(path="/root/kept.py", size=1, mtime=1.0)]
    deleted = find_deleted(existing, scanned)
    assert deleted == [prev]


def _stamp(**overrides: str | None) -> VersionStamp:
    defaults: dict[str, str | None] = dict(
        parser_version="p1",
        chunker_version="c1",
        embedding_model_id="m1",
        embedding_text_version="e1",
    )
    defaults.update(overrides)
    return VersionStamp(**defaults)  # type: ignore[arg-type]


def test_decide_reprocessing_is_none_when_every_stamp_matches() -> None:
    assert decide_reprocessing(_stamp(), _stamp()) is ReprocessDecision.NONE


def test_decide_reprocessing_is_full_when_chunker_version_differs() -> None:
    existing = _stamp(chunker_version="c1")
    current = _stamp(chunker_version="c2")
    assert decide_reprocessing(existing, current) is ReprocessDecision.FULL


def test_decide_reprocessing_is_full_when_parser_version_differs() -> None:
    existing = _stamp(parser_version="p1")
    current = _stamp(parser_version="p2")
    assert decide_reprocessing(existing, current) is ReprocessDecision.FULL


def test_decide_reprocessing_is_embeddings_only_when_only_embedding_model_differs() -> None:
    existing = _stamp(embedding_model_id="m1")
    current = _stamp(embedding_model_id="m2")
    assert decide_reprocessing(existing, current) is ReprocessDecision.EMBEDDINGS_ONLY


def test_decide_reprocessing_is_embeddings_only_when_only_embedding_text_version_differs() -> None:
    existing = _stamp(embedding_text_version="e1")
    current = _stamp(embedding_text_version="e2")
    assert decide_reprocessing(existing, current) is ReprocessDecision.EMBEDDINGS_ONLY


def test_decide_reprocessing_prefers_full_over_embeddings_only_when_both_differ() -> None:
    existing = _stamp(chunker_version="c1", embedding_model_id="m1")
    current = _stamp(chunker_version="c2", embedding_model_id="m2")
    assert decide_reprocessing(existing, current) is ReprocessDecision.FULL


def test_decide_reprocessing_never_treats_a_null_stored_stamp_as_stale() -> None:
    """A file indexed before this tracking existed (or by a processor
    with no version provider) has every stamp field ``None`` -- that must
    never, by itself, force a rebuild, or shipping this tracking
    infrastructure would force a full reindex of every already-indexed
    project.
    """
    existing = VersionStamp(
        parser_version=None,
        chunker_version=None,
        embedding_model_id=None,
        embedding_text_version=None,
    )
    current = _stamp()
    assert decide_reprocessing(existing, current) is ReprocessDecision.NONE
