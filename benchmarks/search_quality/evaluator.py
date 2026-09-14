"""Runs ``benchmarks/search/golden_queries.yaml`` against a real,
already-indexed project and computes Recall@1/3/5/10, MRR, and NDCG@10
(``benchmarks/search/quality.py``) overall and broken down per
``category`` -- the one evaluation both the always-on quality regression
test (``tests/integration/test_search_quality.py``) and the baseline
report generator (``report.py``) build on, so there is exactly one place
that decides what "relevant, found" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

import yaml
from benchmarks.search.quality import ndcg_at_k, recall_at_k, reciprocal_rank, result_key

from ragpilot.core.lifecycle import AppContext
from ragpilot.retrieval import lexical

GOLDEN_QUERIES_PATH = (
    Path(__file__).resolve().parents[1] / "search" / "golden_queries.yaml"
)

_K_VALUES = (1, 3, 5, 10)


@dataclass(frozen=True)
class QueryEvaluation:
    """One golden query's own metrics -- the raw material every
    aggregate (overall or per-category) below is computed from.
    """

    query: str
    category: str
    retrieved: list[str]
    relevant: set[str]
    recall_at: dict[int, float]
    reciprocal_rank: float
    ndcg_at_10: float


@dataclass(frozen=True)
class MetricSummary:
    """Mean metrics across some group of queries (overall, or one
    category) -- ``count`` is carried alongside so a report can show how
    much a given category's numbers are actually backed by.
    """

    count: int
    recall_at: dict[int, float]
    mrr: float
    ndcg_at_10: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "recall_at": {str(k): round(v, 4) for k, v in self.recall_at.items()},
            "mrr": round(self.mrr, 4),
            "ndcg_at_10": round(self.ndcg_at_10, 4),
        }


@dataclass(frozen=True)
class QualityReport:
    overall: MetricSummary
    by_category: dict[str, MetricSummary]
    queries: list[QueryEvaluation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.to_dict(),
            "by_category": {
                category: summary.to_dict()
                for category, summary in sorted(self.by_category.items())
            },
        }


def load_golden_queries(path: Path = GOLDEN_QUERIES_PATH) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"no golden queries loaded from {path}")
    return data


def _summarize(evaluations: list[QueryEvaluation]) -> MetricSummary:
    return MetricSummary(
        count=len(evaluations),
        recall_at={k: mean(e.recall_at[k] for e in evaluations) for k in _K_VALUES},
        mrr=mean(e.reciprocal_rank for e in evaluations),
        ndcg_at_10=mean(e.ndcg_at_10 for e in evaluations),
    )


def evaluate_golden_queries(
    ctx: AppContext, *, project_root: Path, golden: list[dict[str, Any]] | None = None
) -> QualityReport:
    """Runs every golden query through the real ``retrieval/
    lexical.search`` against ``ctx`` and scores it against its own
    ``expected`` set.

    ``project_root`` is the indexed fixture's own root -- every search
    result's absolute path is normalized relative to it before being
    turned into a ``result_key``, matching how the golden queries'
    ``expected`` paths are written (see ``golden_queries.yaml``'s header).
    """
    golden = golden if golden is not None else load_golden_queries()
    evaluations: list[QueryEvaluation] = []

    for item in golden:
        results = lexical.search(ctx, item["query"], limit=10)
        retrieved = [
            result_key(r.kind, Path(r.path).relative_to(project_root).as_posix()) for r in results
        ]
        relevant = {result_key(e["kind"], e["path"]) for e in item["expected"]}
        evaluations.append(
            QueryEvaluation(
                query=item["query"],
                category=item["category"],
                retrieved=retrieved,
                relevant=relevant,
                recall_at={k: recall_at_k(retrieved, relevant, k=k) for k in _K_VALUES},
                reciprocal_rank=reciprocal_rank(retrieved, relevant),
                ndcg_at_10=ndcg_at_k(retrieved, relevant, k=10),
            )
        )

    by_category: dict[str, list[QueryEvaluation]] = {}
    for evaluation in evaluations:
        by_category.setdefault(evaluation.category, []).append(evaluation)

    return QualityReport(
        overall=_summarize(evaluations),
        by_category={category: _summarize(group) for category, group in by_category.items()},
        queries=evaluations,
    )


def format_report(report: QualityReport) -> str:
    lines = [
        f"Golden query set ({report.overall.count} queries)",
        (
            f"Overall: Recall@1={report.overall.recall_at[1]:.3f} "
            f"Recall@3={report.overall.recall_at[3]:.3f} "
            f"Recall@5={report.overall.recall_at[5]:.3f} "
            f"Recall@10={report.overall.recall_at[10]:.3f} "
            f"MRR={report.overall.mrr:.3f} NDCG@10={report.overall.ndcg_at_10:.3f}"
        ),
        "",
        f"{'Category':<22}{'N':<5}{'R@1':<8}{'R@3':<8}{'R@5':<8}{'R@10':<8}{'MRR':<8}{'NDCG@10'}",
    ]
    for category, summary in sorted(report.by_category.items()):
        lines.append(
            f"{category:<22}{summary.count:<5}"
            f"{summary.recall_at[1]:<8.3f}{summary.recall_at[3]:<8.3f}"
            f"{summary.recall_at[5]:<8.3f}{summary.recall_at[10]:<8.3f}"
            f"{summary.mrr:<8.3f}{summary.ndcg_at_10:.3f}"
        )
    return "\n".join(lines)
