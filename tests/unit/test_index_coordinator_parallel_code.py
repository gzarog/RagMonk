"""``IndexCoordinator``'s bounded parallel code extraction (indexing
optimization plan, Phase P4): opt-in via ``indexing.
code_extraction_workers`` (default 1, fully serial). Serial and
parallel runs over the same project must produce equivalent entities/
relationships; a poisoned file must still be isolated without aborting
the rest; concurrency must actually stay within the configured bound;
and a file that changes mid-extraction must be safely retried rather
than silently published.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.code.processor import code_processor, prepare_code, publish_code
from ragmonk.core.config import IndexingConfig, RagMonkConfig
from ragmonk.core.models import FileKind
from ragmonk.indexing.coordinator import IndexCoordinator, ProcessorRegistry, raw_processor
from ragmonk.indexing.runner import build_processor_registry
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import entities_repo, files_repo, relationships_repo
from ragmonk.storage.sqlite import connect


def _config(*, code_extraction_workers: int = 1) -> RagMonkConfig:
    config = RagMonkConfig(
        indexing=IndexingConfig(code_extraction_workers=code_extraction_workers)
    )
    disabled_documents = config.documents.model_copy(update={"enabled": False})
    return config.model_copy(update={"documents": disabled_documents})


def _code_only_registry(*, prepare=prepare_code, publish=publish_code) -> ProcessorRegistry:  # noqa: ANN001
    """A registry that only registers CODE (with the given prepare/
    publish, defaulting to the real ones) and falls back to
    ``raw_processor`` for every other kind -- lets a test inject a spy
    prepare function directly, with no monkeypatch needed.
    """
    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    registry.register(FileKind.CODE, code_processor, prepare=prepare, publish=publish)
    return registry


def _write_project(root: Path, n_files: int) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    for i in range(n_files):
        (pkg / f"mod_{i}.py").write_text(
            f"from pkg.mod_{(i + 1) % n_files} import helper_{(i + 1) % n_files}\n\n\n"
            f"def helper_{i}():\n    return helper_{(i + 1) % n_files}()\n"
        )


def _all_relationships(conn) -> list:  # noqa: ANN001, ANN201
    return [
        r
        for e in entities_repo.list_all(conn)
        for r in relationships_repo.list_by_file(conn, e.file_id)
    ]


def test_serial_and_parallel_produce_equivalent_entities_and_relationships(
    tmp_path: Path,
) -> None:
    serial_root = tmp_path / "serial"
    parallel_root = tmp_path / "parallel"
    _write_project(serial_root, 6)
    _write_project(parallel_root, 6)

    serial_conn = connect(tmp_path / "serial.db")
    parallel_conn = connect(tmp_path / "parallel.db")
    try:
        apply_migrations(serial_conn, "knowledge")
        apply_migrations(parallel_conn, "knowledge")

        serial_registry = build_processor_registry(_config(code_extraction_workers=1))
        parallel_registry = build_processor_registry(_config(code_extraction_workers=4))

        serial_coord = IndexCoordinator(
            serial_conn,
            "s1",
            str(serial_root),
            [],
            [],
            _config(code_extraction_workers=1),
            processors=serial_registry,
        )
        parallel_coord = IndexCoordinator(
            parallel_conn,
            "s1",
            str(parallel_root),
            [],
            [],
            _config(code_extraction_workers=4),
            processors=parallel_registry,
        )
        serial_result = serial_coord.run()
        parallel_result = parallel_coord.run()

        assert serial_result.indexed == parallel_result.indexed == 6
        assert serial_result.failed == parallel_result.failed == 0

        serial_entities = sorted(
            (e.qualified_name, e.kind.value) for e in entities_repo.list_all(serial_conn)
        )
        parallel_entities = sorted(
            (e.qualified_name, e.kind.value) for e in entities_repo.list_all(parallel_conn)
        )
        assert serial_entities == parallel_entities

        serial_rels = sorted(
            (r.relationship_type.value, r.resolver, r.confidence.value)
            for r in _all_relationships(serial_conn)
        )
        parallel_rels = sorted(
            (r.relationship_type.value, r.resolver, r.confidence.value)
            for r in _all_relationships(parallel_conn)
        )
        assert serial_rels == parallel_rels
    finally:
        serial_conn.close()
        parallel_conn.close()


def test_a_poisoned_file_is_isolated_under_parallel_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real retry/backoff needs several index runs before a failure is
    # permanent; force it on the first attempt so this test can assert
    # `failed` within one `coord.run()` call, matching
    # tests/integration/test_code_indexing.py's own convention.
    monkeypatch.setattr(coordinator_module.retry, "is_permanent", lambda attempt: True)

    root = tmp_path / "source"
    root.mkdir()
    (root / "good_a.py").write_text("def a():\n    return 1\n")
    (root / "broken.py").write_text("def broken(:\n    pass\n")  # syntax error
    (root / "good_b.py").write_text("def b():\n    return 2\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(code_extraction_workers=3)
        registry = build_processor_registry(config)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        assert result.failed == 1
        assert result.indexed == 2
        remaining = {f.path: f.status.value for f in files_repo.list_by_source(conn, "s1")}
        assert any(p.endswith("broken.py") and s == "failed" for p, s in remaining.items())
        assert any(p.endswith("good_a.py") and s == "indexed" for p, s in remaining.items())
        assert any(p.endswith("good_b.py") and s == "indexed" for p, s in remaining.items())
    finally:
        conn.close()


def test_concurrency_stays_within_the_configured_worker_bound(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _write_project(root, 12)

    max_concurrent = 0
    current = 0
    lock = threading.Lock()

    def spying_prepare_code(path: Path, source_root: Path):  # noqa: ANN202
        nonlocal max_concurrent, current
        with lock:
            current += 1
            max_concurrent = max(max_concurrent, current)
        try:
            time.sleep(0.02)  # widen the window so overlap is observable
            return prepare_code(path, source_root)
        finally:
            with lock:
                current -= 1

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(code_extraction_workers=3)
        registry = _code_only_registry(prepare=spying_prepare_code)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        assert result.indexed == 12
        assert max_concurrent <= 3
        assert max_concurrent > 1  # actually ran concurrently, not accidentally serial
    finally:
        conn.close()


def test_file_changed_during_parallel_processing_is_safely_retried(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    target = root / "a.py"
    target.write_text("def a():\n    return 1\n")
    (root / "b.py").write_text("def b():\n    return 2\n")

    def mutating_prepare_code(path: Path, source_root: Path):  # noqa: ANN202
        if path.name == "a.py":
            # Simulate a concurrent write landing between claim and
            # publish: the coordinator captured a.py's identity before
            # this call; mutate it now so publish's post-check sees a
            # mismatch.
            time.sleep(0.01)
            path.write_text("def a():\n    return 999\n")
        return prepare_code(path, source_root)

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        config = _config(code_extraction_workers=2)
        registry = _code_only_registry(prepare=mutating_prepare_code)
        coord = IndexCoordinator(conn, "s1", str(root), [], [], config, processors=registry)
        result = coord.run()

        records = {f.path: f for f in files_repo.list_by_source(conn, "s1")}
        a_record = next(f for p, f in records.items() if p.endswith("a.py"))
        assert a_record.status.value in {"retry", "queued"}
        assert result.indexed == 1  # only b.py made it through this pass
    finally:
        conn.close()
