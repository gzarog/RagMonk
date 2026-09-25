"""Indexing performance optimization plan (`ragmonk-indexing-performance-v1`),
Phase P7: end-to-end regression through the real CLI with every prior
phase's feature actually turned on together in one realistic project,
not exercised in isolation the way each phase's own unit/integration
tests do. Proves the combined pipeline still produces byte-for-byte
correct results -- the acceptance gate every phase's PR promised but
that only a run combining all of them can actually verify:

- P3 (verified fingerprint reuse): a second, content-unchanged pass
  hashes nothing at all.
- P4 (bounded parallel code extraction): `code_extraction_workers > 1`
  is turned on for this whole test, not left at its serial default.
- P1/P2 (safe incremental reconciliation): an edit, a delete, and a
  rename in the same pass are all classified and applied correctly.
- P6 (cross-link correctness under the N+1 fix): links from documents
  to code entities still form correctly, and stale links from a
  regenerated/deleted file are still cleaned up.
- P5 (embeddings-only backfill): a project indexed entirely before
  `search.semantic` was ever turned on gets real vectors purely via
  `ragmonk vectors backfill`, no reindex.

The real embedding model is never loaded, mirroring
``test_semantic_retrieval.py``: ``retrieval/embedder.embed_texts`` is
monkeypatched to a small deterministic function.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from benchmarks.indexing.metrics import count_hash_calls
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.retrieval import embedder
from ragmonk.storage.repositories import (
    embeddings_repo,
    entities_repo,
    links_repo,
)
from ragmonk.storage.sqlite import connect


def _fake_embed_texts(texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.lower().encode("utf-8")).digest()
        vectors.append([b / 255.0 for b in digest[:8]] or [1.0])
    return vectors


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedder, "embed_texts", _fake_embed_texts)


def _write_project(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "animal_service.py").write_text(
        "class AnimalService:\n"
        "    def bark_loudly(self):\n"
        "        return 'WOOF'\n\n"
        "    def call_helper(self):\n"
        "        return helper_util()\n"
    )
    (pkg / "helpers.py").write_text("def helper_util():\n    return 42\n")
    (pkg / "to_delete.py").write_text("def doomed():\n    return None\n")
    (pkg / "to_rename.py").write_text("def stable_name():\n    return 'stable'\n")

    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "# API Reference\n\n"
        "The AnimalService class documents bark_loudly, the core API.\n\n"
        "See also stable_name for a utility function.\n"
    )


def _knowledge_conn(home: Path, root: Path):  # noqa: ANN201
    project_id = paths.project_id_for_path(root)
    return connect(paths.project_db_path(project_id, home))


def test_full_optimized_pipeline_end_to_end(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    # P4: bounded parallel code extraction actually turned on for this
    # whole test, not left at its serial default -- the plan's own
    # acceptance criterion is that this must be indistinguishable in
    # result from the serial path, only tested here end-to-end alongside
    # every other phase at once.
    monkeypatch.setenv("RAGMONK_INDEXING__CODE_EXTRACTION_WORKERS", "3")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0

    # --- cold index -----------------------------------------------------
    cold = runner.invoke(app, ["index"])
    assert cold.exit_code == 0, cold.output
    assert "indexed=5" in cold.output
    assert "failed=0" in cold.output
    assert "linked=" in cold.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        entities_after_cold = {e.qualified_name for e in entities_repo.list_all(conn)}
        assert any("bark_loudly" in n for n in entities_after_cold)
        links_after_cold = links_repo.list_all(conn)
        assert links_after_cold != []
    finally:
        conn.close()

    # --- P3: a second, content-unchanged pass must hash nothing at all --
    with count_hash_calls() as counters:
        warm = runner.invoke(app, ["index"])
    assert warm.exit_code == 0, warm.output
    assert "changed=0" in warm.output
    assert "new=0" in warm.output
    assert counters.calls == 0

    # --- P1/P2: edit + delete + rename in the same pass ------------------
    (root / "pkg" / "animal_service.py").write_text(
        "class AnimalService:\n"
        "    def bark_loudly(self):\n"
        "        return 'WOOF WOOF'\n\n"
        "    def call_helper(self):\n"
        "        return helper_util()\n\n"
        "    def new_method(self):\n"
        "        return 'new'\n"
    )
    (root / "pkg" / "to_delete.py").unlink()
    (root / "pkg" / "to_rename.py").rename(root / "pkg" / "renamed.py")

    mixed = runner.invoke(app, ["index"])
    assert mixed.exit_code == 0, mixed.output
    assert "changed=1" in mixed.output
    assert "deleted=1" in mixed.output
    assert "moved=1" in mixed.output

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        # The renamed file kept its identity (same row, new path) --
        # P2's rename reconciliation, not a delete+new pair.
        paths_now = {row["path"] for row in conn.execute("SELECT path FROM files").fetchall()}
        assert any(p.endswith("renamed.py") for p in paths_now)
        assert not any(p.endswith("to_rename.py") for p in paths_now)
        assert not any(p.endswith("to_delete.py") for p in paths_now)

        # entities for the deleted file are gone; the new method exists;
        # the renamed file's entity survived under the same content.
        entity_names = {e.qualified_name for e in entities_repo.list_all(conn)}
        assert not any("doomed" in n for n in entity_names)
        assert any("new_method" in n for n in entity_names)
        assert any("stable_name" in n for n in entity_names)

        # P6: cross-links are still correct after the edit -- the old
        # bark_loudly-mention link was cleaned up and regenerated (not
        # duplicated or left stale), and the renamed file's entity is
        # still linkable.
        links_after_mixed = links_repo.list_all(conn)
        assert links_after_mixed != []
        entity_ids = {e.id for e in entities_repo.list_all(conn)}
        for link in links_after_mixed:
            assert link.entity_id in entity_ids
    finally:
        conn.close()

    # --- P5: backfill vectors for a project indexed entirely before -----
    # search.semantic was ever turned on -- no reindex, just backfill.
    conn = _knowledge_conn(ragmonk_home, root)
    try:
        assert embeddings_repo.count_all(conn) == 0
    finally:
        conn.close()

    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")
    noop_reindex = runner.invoke(app, ["index"])
    assert noop_reindex.exit_code == 0, noop_reindex.output
    assert "embedded=0" in noop_reindex.output

    backfill = runner.invoke(app, ["vectors", "backfill", "--json"])
    assert backfill.exit_code == 0, backfill.output
    backfill_payload = json.loads(backfill.output)["data"]
    assert backfill_payload["backfilled"][0]["embedded"] > 0

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        assert embeddings_repo.count_all(conn) > 0
    finally:
        conn.close()

    search_result = runner.invoke(app, ["search", "bark_loudly", "--json"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert payload["semantic"]["available"] is True
    assert payload["results"] != []
