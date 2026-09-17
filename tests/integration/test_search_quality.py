"""Golden-query quality regression tests (blueprint section 37; Phase 0
of the search-quality improvement plan): index a small, fixed fixture
project through the real CLI pipeline (Tree-sitter parsing, FTS
indexing -- no synthetic corpus shortcuts, unlike ``benchmarks/search``'s
latency suite), run every query in
``benchmarks/search/golden_queries.yaml`` through the real
``retrieval/lexical.search``, and assert Recall@1/3/5/10, MRR, and
NDCG@10 (``benchmarks/search_quality/evaluator.py``) stay at the levels
this fixture is known to support.

Deliberately lexical-only: every golden query below is answerable by
lexical search alone (the fixture's document text literally shares
vocabulary with each "semantic_document"-style query), so this stays in
the default, network/model-free test suite -- semantic/hybrid search has
its own separate, already-covered contract (see
``tests/integration/test_semantic_retrieval.py``).

Always-on and blocking (no pytest marker, unlike
``benchmark_search``/``docling_pdf``/etc.): fast, offline, and fully
deterministic, so a change to lexical ranking that regresses retrieval
quality is caught here rather than only noticed by a human eyeballing
search output -- "search changes cannot be merged without running the
benchmark suite."
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.search_quality.evaluator import (
    GOLDEN_QUERIES_PATH,
    evaluate_golden_queries,
    format_report,
    load_golden_queries,
)
from benchmarks.search_quality.fixture_project import write_project
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core.lifecycle import AppContext

# This fixture's own known-achievable baseline (see
# ``benchmarks/search_quality/baseline_report.json``, generated from this
# exact fixture/golden-query set), pinned with a small margin below the
# measured numbers so a real regression in lexical ranking fails here
# rather than only being noticed by a human eyeballing search output.
# The blueprint states quality targets qualitatively ("speed must not
# reduce retrieval quality"), not as fixed numbers -- these are this
# project's own concrete floor.
_MIN_RECALL_AT_5 = 0.95
_MIN_RECALL_AT_10 = 1.0
_MIN_MRR = 0.80
_MIN_NDCG_AT_10 = 0.85

# Per-category floors (Recall@5), each set with margin below this
# fixture's own measured baseline for that category -- some categories
# (code_to_document, file_path_lookup) are genuinely harder for today's
# lexical-only ranking and score lower on purpose (see
# ``golden_queries.yaml``'s header comment on `expected`), so one
# overall floor above would either miss a regression in a strong
# category or be unmeetable for a weak one.
_MIN_RECALL_AT_5_BY_CATEGORY: dict[str, float] = {
    "code_to_document": 0.6,
    "cross_document": 0.7,
    "exact_heading_lookup": 1.0,
    "exact_symbol_lookup": 1.0,
    "exact_title_lookup": 1.0,
    "file_path_lookup": 0.5,
    "keyword_search": 0.85,
    "semantic_document": 0.85,
    "table_question": 0.85,
    "typo_partial_term": 0.75,
}


def _index_fixture_project(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path / "project"
    write_project(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(root)]).exit_code == 0
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output
    return root


def test_golden_query_set_meets_quality_thresholds(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _index_fixture_project(ragmonk_home, runner, tmp_path, monkeypatch)

    ctx = AppContext.bootstrap(home=ragmonk_home, cwd=tmp_path)
    try:
        report = evaluate_golden_queries(ctx, project_root=root)
        print("\n" + format_report(report))

        for evaluation in report.queries:
            assert evaluation.recall_at[5] > 0, (
                f"query {evaluation.query!r} ({evaluation.category}) found none of its "
                f"expected relevant results within the top 5 "
                f"(retrieved: {evaluation.retrieved[:5]}, relevant: {evaluation.relevant})"
            )

        assert report.overall.recall_at[5] >= _MIN_RECALL_AT_5
        assert report.overall.recall_at[10] >= _MIN_RECALL_AT_10
        assert report.overall.mrr >= _MIN_MRR
        assert report.overall.ndcg_at_10 >= _MIN_NDCG_AT_10
    finally:
        ctx.close()


def test_golden_query_set_per_category_recall_floor(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-category counterpart to the overall-metrics test above:
    a category with genuinely fewer/harder queries can hide a real
    regression inside an otherwise-healthy overall average, so each
    category is also held to its own floor.
    """
    root = _index_fixture_project(ragmonk_home, runner, tmp_path, monkeypatch)

    golden = load_golden_queries()
    categories = {item["category"] for item in golden}
    assert categories >= set(_MIN_RECALL_AT_5_BY_CATEGORY), (
        "a category was added to golden_queries.yaml without a matching floor in "
        f"{__name__}._MIN_RECALL_AT_5_BY_CATEGORY: {categories - set(_MIN_RECALL_AT_5_BY_CATEGORY)}"
    )

    ctx = AppContext.bootstrap(home=ragmonk_home, cwd=tmp_path)
    try:
        report = evaluate_golden_queries(ctx, project_root=root, golden=golden)
        failures = [
            f"{category}: Recall@5={summary.recall_at[5]:.3f} < floor "
            f"{_MIN_RECALL_AT_5_BY_CATEGORY[category]:.3f}"
            for category, summary in report.by_category.items()
            if summary.recall_at[5] < _MIN_RECALL_AT_5_BY_CATEGORY[category]
        ]
        assert not failures, "category Recall@5 below floor:\n" + "\n".join(failures)
    finally:
        ctx.close()


def test_golden_queries_path_is_expected_location() -> None:
    """Guards against the golden query file silently moving without this
    test suite (and CONTRIBUTING.md's documented location) following.
    """
    assert GOLDEN_QUERIES_PATH.name == "golden_queries.yaml"
    assert GOLDEN_QUERIES_PATH.is_file()
