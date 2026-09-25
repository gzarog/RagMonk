"""Indexing optimization plan V2, Phase P6: the V2-specific scenarios
the small/medium tiered suite (``__main__.py``, inherited from the P0-P7
predecessor plan) doesn't cover on its own -- document-heavy indexing,
an embedding backfill (``search.semantic`` freshly turned on), and
concurrent document extraction (V2 Phase P2's ``document_extraction_
workers`` > 1) compared against the serial default.

Run as ``python -m benchmarks.indexing.v2_supplemental [--out path.json]``.
Uses the *real* embedding model (``retrieval/embedder.py``) -- unlike
the tiered suite's own document-conversion path, this one specifically
needs it, since P3's cache-reuse and P2's concurrency both only matter
when ``search.semantic`` is actually on. Requires the pinned model's
weights to already be cached locally (this project's own offline-first
convention -- see ``retrieval/embedder.py``'s module docstring); if
loading fails, the affected scenario is skipped and recorded as such in
the output JSON rather than crashing the whole run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.indexing import fixtures
from benchmarks.indexing.metrics import environment_info

from ragmonk.core.config import IndexingConfig, RagMonkConfig, SearchConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing import runner as index_runner
from ragmonk.retrieval import embedder
from ragmonk.sources.registry import SourceRegistry


def _embedder_available() -> bool:
    try:
        embedder.embed_texts(["warmup"])
        return True
    except embedder.EmbeddingModelUnavailableError:
        return False


def _run_pass(home: Path, source_root: Path, config: RagMonkConfig) -> tuple[float, object]:
    os.environ["RAGMONK_UPDATES__ENABLED"] = "false"
    ctx = AppContext.bootstrap(home=home, cwd=source_root, cli_overrides=None)
    try:
        ctx.config = config
        registry = SourceRegistry(ctx.sources_conn, home=home)
        source = registry.add(str(source_root))
        processors = index_runner.build_processor_registry(config)
        started = time.perf_counter()
        pass_result = index_runner.run_source_pass(ctx, source, processors)
        elapsed = time.perf_counter() - started
        return elapsed, pass_result
    finally:
        ctx.close()


def _document_heavy_scenario(n_docs: int) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="ragmonk_bench_v2_dochevy_"))
    try:
        source_root = workdir / "source"
        source_root.mkdir(parents=True)
        fixtures.generate_mixed_document_corpus(source_root, n_docs, subdir=".")
        config = RagMonkConfig(indexing=IndexingConfig())
        wall, pass_result = _run_pass(workdir / "home", source_root, config)
        return {
            "scenario": "document_heavy",
            "n_docs": n_docs,
            "wall_time_s": wall,
            "indexed": pass_result.result.indexed,
            "failed": pass_result.result.failed,
            "process_seconds": pass_result.result.timings.process_seconds,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _embedding_backfill_scenario(n_docs: int) -> dict:
    """A project already indexed with ``search.semantic`` off, then
    turned on -- this project's own documented tradeoff (``indexing/
    embedding_indexer.py``'s module docstring) is that a normal
    ``ragmonk index`` pass will *never* revisit an already-indexed,
    content-unchanged file for this reason alone (its stored
    ``embedding_model_id`` is ``NULL``, which ``decide_reprocessing``
    deliberately never treats as "stale" -- see that function's own
    docstring), so the real backfill path is the dedicated ``ragmonk
    vectors backfill`` command (``files_repo.list_missing_embeddings`` +
    a direct ``prepare_embeddings``/``publish_embeddings`` call, exactly
    what ``cli/vectors.py``'s ``backfill()`` does) -- reproduced here
    directly rather than shelling out to the CLI.
    """
    if not _embedder_available():
        return {"scenario": "embedding_backfill", "skipped": True, "reason": "embedder unavailable"}

    from ragmonk.core import paths as core_paths
    from ragmonk.indexing.embedding_indexer import prepare_embeddings, publish_embeddings
    from ragmonk.storage.repositories import files_repo
    from ragmonk.storage.sqlite import transaction

    workdir = Path(tempfile.mkdtemp(prefix="ragmonk_bench_v2_embed_"))
    try:
        source_root = workdir / "source"
        source_root.mkdir(parents=True)
        fixtures.generate_mixed_document_corpus(source_root, n_docs, subdir=".")

        off_config = RagMonkConfig(search=SearchConfig(semantic=False))
        _wall, cold_result = _run_pass(workdir / "home", source_root, off_config)

        on_config = RagMonkConfig(search=SearchConfig(semantic=True))
        ctx = AppContext.bootstrap(home=workdir / "home", cwd=source_root, cli_overrides=None)
        try:
            ctx.config = on_config
            project_id = core_paths.project_id_for_path(source_root)
            conn = ctx.project_conn(project_id)
            missing = files_repo.list_missing_embeddings(
                conn, cold_result.source.id, model_id=embedder.EMBEDDING_MODEL_ID
            )
            code_ids = [f.id for f in missing if f.kind.value == "code"]
            document_ids = [f.id for f in missing if f.kind.value == "document"]
            started = time.perf_counter()
            prepared = prepare_embeddings(
                conn,
                source_id=cold_result.source.id,
                touched_code_file_ids=code_ids,
                touched_document_file_ids=document_ids,
            )
            embedded = 0
            if prepared is not None:
                with transaction(conn):
                    embedded = publish_embeddings(conn, prepared)
            wall = time.perf_counter() - started
        finally:
            ctx.close()

        return {
            "scenario": "embedding_backfill",
            "n_docs": n_docs,
            "missing_before_backfill": len(missing),
            "wall_time_s": wall,
            "embedded": embedded,
            "cache_reused": prepared.cache_reused if prepared is not None else 0,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _embedding_cache_reuse_scenario(n_docs: int) -> dict:
    """V2 Phase P3's real point: identical text embedded in two
    *separate* projects (two separate ``knowledge.db`` files, exactly
    this project's real project-isolation boundary) -- the second
    project's pass over the same generated corpus is a fresh model run
    per Phase P3's own project-isolation guarantee (no cross-project
    reuse), so this instead measures reuse *within* one project across
    two passes forced to re-touch the same content via an
    embeddings-only version-stamp bump-and-revert. Simpler and more
    direct: reuse embed_touched_files' own touched-file re-embed path by
    forcing a fresh backfill pass with the exact same corpus content
    twice in the *same* project, using ``ragmonk vectors backfill``'s
    own "files missing this model's embeddings" query after the first
    pass wiped them (simulating a rebuild).
    """
    if not _embedder_available():
        return {
            "scenario": "embedding_cache_reuse",
            "skipped": True,
            "reason": "embedder unavailable",
        }

    from ragmonk.core import paths as core_paths
    from ragmonk.indexing.embedding_indexer import prepare_embeddings, publish_embeddings
    from ragmonk.storage.repositories import files_repo
    from ragmonk.storage.sqlite import transaction

    workdir = Path(tempfile.mkdtemp(prefix="ragmonk_bench_v2_cachereuse_"))
    try:
        source_root = workdir / "source"
        source_root.mkdir(parents=True)
        fixtures.generate_mixed_document_corpus(source_root, n_docs, subdir=".")

        on_config = RagMonkConfig(search=SearchConfig(semantic=True))
        first_wall, first_result = _run_pass(workdir / "home", source_root, on_config)

        # Simulate "ragmonk vectors backfill"'s real trigger (a vector
        # rebuild, or a model re-pin): null every file's embedding
        # version stamp so files_repo.list_missing_embeddings picks them
        # all back up, then run the exact same prepare_embeddings/
        # publish_embeddings pair that command itself calls (see
        # cli/vectors.py's backfill()) -- content is untouched, so this
        # is exactly Phase P3's cache-hit path (same text, same
        # model/preprocessing/version identity as the first pass).
        ctx = AppContext.bootstrap(home=workdir / "home", cwd=source_root, cli_overrides=None)
        try:
            ctx.config = on_config
            project_id = core_paths.project_id_for_path(source_root)
            conn = ctx.project_conn(project_id)
            with transaction(conn):
                conn.execute(
                    "UPDATE files SET embedding_model_id = NULL, embedding_text_version = NULL "
                    "WHERE source_id = ?",
                    (first_result.source.id,),
                )
            missing = files_repo.list_missing_embeddings(
                conn, first_result.source.id, model_id=embedder.EMBEDDING_MODEL_ID
            )
            code_ids = [f.id for f in missing if f.kind.value == "code"]
            document_ids = [f.id for f in missing if f.kind.value == "document"]
            second_started = time.perf_counter()
            prepared = prepare_embeddings(
                conn, source_id=first_result.source.id,
                touched_code_file_ids=code_ids, touched_document_file_ids=document_ids,
            )
            second_embedded = 0
            second_cache_reused = 0
            if prepared is not None:
                with transaction(conn):
                    second_embedded = publish_embeddings(conn, prepared)
                second_cache_reused = prepared.cache_reused
            second_wall = time.perf_counter() - second_started
        finally:
            ctx.close()

        return {
            "scenario": "embedding_cache_reuse",
            "n_docs": n_docs,
            "first_pass_wall_time_s": first_wall,
            "first_pass_embedded": first_result.embedded,
            "first_pass_cache_reused": first_result.embedding_cache_reused,
            "second_pass_wall_time_s": second_wall,
            "second_pass_embedded": second_embedded,
            "second_pass_cache_reused": second_cache_reused,
            "second_pass_missing_before_backfill": len(missing),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _concurrent_document_extraction_scenario(n_docs: int) -> dict:
    """V2 Phase P2: serial (``document_extraction_workers=1``, the
    default) vs. concurrent (``=4``) document extraction over the same
    generated corpus -- real Docling conversion (plain-text/HTML/CSV/
    Markdown backends, no ML model download needed for these formats),
    not a mocked processor.
    """
    results: dict[str, dict] = {}
    for workers in (1, 4):
        workdir = Path(tempfile.mkdtemp(prefix=f"ragmonk_bench_v2_docworkers{workers}_"))
        try:
            source_root = workdir / "source"
            source_root.mkdir(parents=True)
            fixtures.generate_mixed_document_corpus(source_root, n_docs, subdir=".")
            config = RagMonkConfig(
                indexing=IndexingConfig(document_extraction_workers=workers)
            )
            wall, pass_result = _run_pass(workdir / "home", source_root, config)
            results[f"workers_{workers}"] = {
                "wall_time_s": wall,
                "indexed": pass_result.result.indexed,
                "failed": pass_result.result.failed,
                "process_seconds": pass_result.result.timings.process_seconds,
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return {"scenario": "concurrent_document_extraction", "n_docs": n_docs, **results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RagMonk V2 supplemental benchmark scenarios")
    parser.add_argument("--n-docs", type=int, default=200)
    parser.add_argument(
        "--out", type=Path, default=Path("benchmarks/indexing/v2_p6_supplemental.json")
    )
    args = parser.parse_args(argv)

    scenarios = [
        _document_heavy_scenario(args.n_docs),
        _embedding_backfill_scenario(args.n_docs),
        _embedding_cache_reuse_scenario(args.n_docs),
        _concurrent_document_extraction_scenario(args.n_docs),
    ]
    for row in scenarios:
        print(json.dumps(row, indent=2))

    payload = {
        "plan_id": "ragmonk-indexing-optimization-v2-completion",
        "phase": "V2-P6-supplemental",
        "generated_at": datetime.now(UTC).isoformat(),
        "n_docs": args.n_docs,
        "environment": environment_info(),
        "scenarios": scenarios,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
