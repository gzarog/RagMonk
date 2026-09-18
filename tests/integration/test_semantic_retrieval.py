"""End-to-end Phase 9 semantic retrieval: index a small project with
``search.semantic`` enabled, verify embeddings actually get computed and
stored, verify ``ragmonk search``/``ragmonk explore`` surface a
semantic-tier result distinctly from lexical/graph results, verify
behavior is unchanged with ``search.semantic`` left at its default
``false``, and verify graceful degradation when embeddings are missing/
cleared.

The real embedding model is never loaded here: ``retrieval/embedder.
embed_texts`` is monkeypatched to a small deterministic hash-based
function, keeping this test in the default, fast suite while still
exercising every real code path around it (indexing, storage, cosine
similarity, CLI wiring) -- see ``tests/unit/test_embedder.py`` /
CONTRIBUTING.md's ``embedding_model`` marker for the real-model tests.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.retrieval import embedder
from ragmonk.storage.repositories import embeddings_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _fake_embed_texts(texts: list[str]) -> list[list[float]]:
    """Deterministic, dependency-free stand-in for the real model: each
    text hashes to a small fixed-dimension vector so semantically
    unrelated calls (e.g. a query vs. indexed content) still produce
    *some* non-degenerate cosine similarity structure to assert on,
    without needing torch/transformers or any real embedding math.
    """
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.lower().encode("utf-8")).digest()
        vectors.append([b / 255.0 for b in digest[:8]] or [1.0])
    return vectors


def _write_project(root: Path) -> None:
    services = root / "services"
    services.mkdir(parents=True)
    (services / "animal_service.py").write_text(
        "class AnimalService:\n    def bark_loudly(self):\n        return 'WOOF'\n"
    )
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "# API Reference\n\nThe AnimalService class documents the animal service.\n"
    )


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedder, "embed_texts", _fake_embed_texts)


def test_semantic_disabled_by_default_leaves_search_and_explore_unaffected(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0
    # search.semantic defaults to false -- no embeddings computed at all.
    assert "embedded=0" in index_result.output

    project_id = paths.project_id_for_path(root)
    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        assert embeddings_repo.count_all(conn) == 0
    finally:
        conn.close()

    search_result = runner.invoke(app, ["search", "AnimalService", "--json"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert "semantic" not in payload
    assert payload["results"] != []

    explore_result = runner.invoke(
        app, ["explore", "documents about animal service", "--json"]
    )
    assert explore_result.exit_code == 0, explore_result.output
    explore_payload = json.loads(explore_result.output)["data"]
    assert explore_payload["semantic_results"] == []
    assert explore_payload["semantic_available"] is None


def test_semantic_enabled_computes_embeddings_and_surfaces_semantic_results(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    assert "embedded=0" not in index_result.output

    project_id = paths.project_id_for_path(root)
    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        stored = embeddings_repo.count_all(conn)
        assert stored > 0
        rows = embeddings_repo.list_by_model(conn, embedder.EMBEDDING_MODEL_ID)
        assert len(rows) == stored
    finally:
        conn.close()

    # The exact same query text as an indexed entity's signature scores a
    # perfect (or near-perfect) match under the fake hash-based embedder,
    # so it is guaranteed to surface as a semantic hit distinct from
    # lexical/graph results.
    search_result = runner.invoke(app, ["search", "bark_loudly", "--json"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert payload["semantic"]["available"] is True
    assert payload["semantic"]["results"] != []
    assert all(r["tier"] == "semantic" for r in payload["semantic"]["results"])
    # Semantic results are additive, never mixed into the lexical list's
    # own tier values.
    assert all(r["tier"] != "semantic" for r in payload["results"])

    # "bark_loudly" alone is a bare identifier -> Strategy.SEMANTIC is
    # only added for document/general-intent queries (see
    # retrieval/planner.py), so use a phrasing that actually routes
    # through it.
    explore_result = runner.invoke(app, ["explore", "documents about animal service", "--json"])
    assert explore_result.exit_code == 0, explore_result.output
    explore_payload = json.loads(explore_result.output)["data"]
    assert explore_payload["semantic_available"] is True
    # Any semantic evidence is folded in at the lowest confidence tier.
    semantic_evidence = [
        e for e in explore_payload["evidence"] if e["source"].startswith("semantic:")
    ]
    assert all(e["confidence"] == "heuristic" for e in semantic_evidence)


def test_semantic_search_surfaces_phase_10_document_formats(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search Quality Improvement Plan, Phase 10's new formats flow through
    semantic search exactly like every earlier format: their real,
    Docling-extracted paragraph/table text gets embedded and stored, and
    is findable as a semantic hit. One representative per backend family
    is enough here -- ODT (rule-based text backend) and CSV (table
    backend) -- since ``embedding_indexer.py`` itself has no per-format
    branching to distinguish (see ``tests/unit/test_embedding_indexer.py``
    for that seam directly); the conversion/normalization/chunking layer
    each format goes through first is already proven per-format in
    ``test_document_normalizer.py``.
    """
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(FIXTURES / "document.odt", root / "document.odt")
    shutil.copy(FIXTURES / "simple.csv", root / "simple.csv")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    assert "embedded=0" not in index_result.output

    project_id = paths.project_id_for_path(root)
    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        assert embeddings_repo.count_all(conn) > 0
    finally:
        conn.close()

    # The exact indexed paragraph text as the query -> a perfect match
    # under the fake hash-based embedder, so it's guaranteed to surface
    # as a semantic hit -- same technique the DOCX/PPTX-era tests above
    # use.
    search_result = runner.invoke(
        app, ["search", "Paragraph in section one.", "--json"]
    )
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert payload["semantic"]["available"] is True
    assert payload["semantic"]["results"] != []


def test_semantic_enabled_degrades_gracefully_when_embeddings_are_cleared(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    project_id = paths.project_id_for_path(root)
    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        assert embeddings_repo.count_all(conn) > 0
        embeddings_repo.clear_all(conn)
        conn.commit()
    finally:
        conn.close()

    # Clearing embeddings must never touch entities/documents, and
    # search/explore must degrade to "no embeddings yet" rather than
    # erroring out.
    search_result = runner.invoke(app, ["search", "AnimalService", "--json"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert payload["results"] != []  # lexical/entities untouched
    assert payload["semantic"]["available"] is True
    assert payload["semantic"]["results"] == []

    explore_result = runner.invoke(app, ["explore", "AnimalService", "--json"])
    assert explore_result.exit_code == 0, explore_result.output


def test_semantic_enabled_degrades_gracefully_when_the_model_is_unavailable(
    ragmonk_home: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    def _raise(texts: list[str]) -> list[list[float]]:
        raise embedder.EmbeddingModelUnavailableError("simulated: no network")

    monkeypatch.setattr(embedder, "embed_texts", _raise)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    # Indexing must not fail just because the embedding model can't load.
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    assert "embedded=0" in index_result.output

    search_result = runner.invoke(app, ["search", "AnimalService", "--json"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert payload["results"] != []
    assert payload["semantic"]["available"] is False
    assert "unavailable" in payload["semantic"]["reason"]


def test_lazy_semantic_skips_semantic_search_on_a_high_confidence_hit(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blueprint section 18: with ``search.lazy_semantic`` on, a query the
    lexical pass already answers with high confidence (an exact symbol
    match here) must never trigger the embedding model at all.
    """
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")
    monkeypatch.setenv("RAGMONK_SEARCH__LAZY_SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    def _fail_if_called(texts: list[str]) -> list[list[float]]:
        raise AssertionError("embedding model must not be invoked for a high-confidence query")

    monkeypatch.setattr(embedder, "embed_texts", _fail_if_called)

    search_result = runner.invoke(app, ["search", "AnimalService", "--json", "--explain"])
    assert search_result.exit_code == 0, search_result.output
    payload = json.loads(search_result.output)["data"]
    assert "semantic" not in payload
    assert payload["explain"]["lexical_confidence"] == "high"
    assert payload["explain"]["semantic_skipped"] is True
    assert all(stage["stage"] != "semantic" for stage in payload["explain"]["stages"])


def test_explain_reports_per_stage_timings(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    result = runner.invoke(app, ["search", "AnimalService", "--json", "--explain"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    explain = payload["explain"]
    assert explain["query_kind"] == "symbol"
    stage_names = {stage["stage"] for stage in explain["stages"]}
    assert {"entities", "documents", "paths", "merge"} <= stage_names
    assert explain["total_ms"] >= 0

    text_result = runner.invoke(app, ["search", "AnimalService", "--explain"])
    assert text_result.exit_code == 0, text_result.output
    assert "Query kind:" in text_result.output
    assert "Total:" in text_result.output


def test_vectors_rebuild_and_doctor_report_the_ann_backend(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blueprint sections 15/32: ``ragmonk vectors rebuild`` regenerates
    the persistent ANN index from SQLite, and ``ragmonk doctor`` reports
    which backend is active and how many vectors it holds.
    """
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    rebuild_result = runner.invoke(app, ["vectors", "rebuild", "--json"])
    assert rebuild_result.exit_code == 0, rebuild_result.output
    payload = json.loads(rebuild_result.output)["data"]
    assert payload["rebuilt"][0]["backend"] == "usearch"
    assert payload["rebuilt"][0]["vectors"] > 0

    from ragmonk.core import paths

    project_id = paths.project_id_for_path(root)
    index_path = paths.project_vector_index_path(project_id, ragmonk_home)
    assert index_path.is_file()

    doctor_result = runner.invoke(app, ["doctor", "--json"])
    assert doctor_result.exit_code == 0, doctor_result.output
    doctor_payload = json.loads(doctor_result.output)["data"]
    semantic_section = next(s for s in doctor_payload["sections"] if s["name"] == "Semantic")
    detail = semantic_section["checks"][0]["detail"]
    assert "usearch" in detail
    assert "vector(s)" in detail


def test_hybrid_flag_merges_and_reranks_without_changing_existing_keys(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blueprint sections 21/22: ``--hybrid`` adds a merged, reranked
    view alongside (never instead of) the existing ``results``/
    ``semantic`` keys. This query's own top hit is an exact symbol match
    (``bark_loudly`` is a real method name), so it lands in Phase 8's
    pinned tier and stays first regardless of fusion -- see
    ``tests/unit/test_merger_and_reranker.py`` for RRF's actual hybrid-
    tier reordering (semantic can now outrank a weak, non-pinned lexical
    hit there).
    """
    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    result = runner.invoke(app, ["search", "bark_loudly", "--hybrid", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["results"] != []
    assert payload["semantic"]["available"] is True
    assert "hybrid" in payload
    assert payload["hybrid"] != []
    lexical_ids = {r["id"] for r in payload["results"]}
    first_hit = payload["hybrid"][0]
    assert first_hit["id"] in lexical_ids
    assert first_hit["tier"] != "semantic_only"

    no_flag_result = runner.invoke(app, ["search", "bark_loudly", "--json"])
    assert no_flag_result.exit_code == 0
    assert "hybrid" not in json.loads(no_flag_result.output)["data"]


def test_reranker_disabled_by_default_leaves_hybrid_output_unchanged(
    ragmonk_home: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search Quality Improvement Plan, Phase 11: ``search.reranker.
    enabled`` defaults to ``false``, so ``--hybrid`` output must be
    byte-identical to the pre-Phase-11 RRF-only ranking -- proven here by
    making the neural pass reverse order if it ever ran (a change that
    would be impossible to miss) and asserting the output is unaffected.
    """
    from ragmonk.retrieval import neural_reranker

    def _reversing_score_batch(_query: str, texts: list[str]) -> list[float]:
        return list(range(len(texts)))

    monkeypatch.setattr(neural_reranker, "score_pairs", _reversing_score_batch)

    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    without_patch_result = runner.invoke(app, ["search", "bark_loudly", "--hybrid", "--json"])
    assert without_patch_result.exit_code == 0, without_patch_result.output
    hybrid_ids = [h["id"] for h in json.loads(without_patch_result.output)["data"]["hybrid"]]

    # search.reranker.enabled is unset (defaults to false) -- the stub
    # above is never even called, so the ranking is unaffected.
    again_result = runner.invoke(app, ["search", "bark_loudly", "--hybrid", "--json"])
    assert again_result.exit_code == 0, again_result.output
    again_ids = [h["id"] for h in json.loads(again_result.output)["data"]["hybrid"]]
    assert again_ids == hybrid_ids


def test_reranker_enabled_reorders_the_hybrid_view_and_falls_back_gracefully(
    ragmonk_home: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``search.reranker.enabled=true``, the neural pass actually
    reorders ``--hybrid``'s top ``top_n`` hits (proven with a stub scorer
    that exactly reverses RRF order, never loading a real model), and its
    stage timing shows up under ``--explain``. A second run whose stub
    raises ``NeuralRerankerUnavailableError`` falls back to the unpatched
    RRF order instead of failing the search.
    """
    from ragmonk.retrieval import neural_reranker

    root = tmp_path / "project"
    _write_project(root)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    # Established with the reranker still at its default (disabled) --
    # this is the RRF-only order graceful fallback must reproduce, and
    # the order the reversing stub below must visibly disturb. Real
    # ``score_pairs`` is never called anywhere in this test.
    baseline_result = runner.invoke(app, ["search", "bark_loudly", "--hybrid", "--json"])
    assert baseline_result.exit_code == 0, baseline_result.output
    baseline_ids = [h["id"] for h in json.loads(baseline_result.output)["data"]["hybrid"]]
    assert len(baseline_ids) >= 2

    monkeypatch.setenv("RAGMONK_SEARCH__RERANKER__ENABLED", "true")

    def _reversing_score_batch(_query: str, texts: list[str]) -> list[float]:
        return list(range(len(texts)))

    monkeypatch.setattr(neural_reranker, "score_pairs", _reversing_score_batch)
    reversed_result = runner.invoke(
        app, ["search", "bark_loudly", "--hybrid", "--json", "--explain"]
    )
    assert reversed_result.exit_code == 0, reversed_result.output
    reversed_payload = json.loads(reversed_result.output)["data"]
    reversed_ids = [h["id"] for h in reversed_payload["hybrid"]]
    assert reversed_ids == list(reversed(baseline_ids))
    stage_names = [s["stage"] for s in reversed_payload["explain"]["stages"]]
    assert "neural_rerank" in stage_names

    def _unavailable_score_batch(_query: str, _texts: list[str]) -> list[float]:
        raise neural_reranker.NeuralRerankerUnavailableError("simulated: no cached weights")

    monkeypatch.setattr(neural_reranker, "score_pairs", _unavailable_score_batch)
    fallback_result = runner.invoke(app, ["search", "bark_loudly", "--hybrid", "--json"])
    assert fallback_result.exit_code == 0, fallback_result.output
    fallback_ids = [h["id"] for h in json.loads(fallback_result.output)["data"]["hybrid"]]
    assert fallback_ids == baseline_ids
