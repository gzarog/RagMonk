"""Unit tests for ``retrieval/context_builder.py``: dedup, priority, and
each of the three budget knobs enforced independently.
"""

from __future__ import annotations

from ragmonk.core.config import ContextConfig
from ragmonk.core.models import Confidence
from ragmonk.knowledge.evidence import Evidence, EvidenceLocation
from ragmonk.retrieval.context_builder import EvidenceItem, GraphPath, build_context


def _item(
    path: str,
    *,
    entity: str = "e",
    confidence: Confidence = Confidence.HIGH,
    line_start: int | None = 1,
    snippet: str = "x",
) -> EvidenceItem:
    return EvidenceItem(
        evidence=Evidence(
            source="test",
            path=path,
            location=EvidenceLocation(line_start=line_start, line_end=line_start),
            entity=entity,
            relationship="calls",
            confidence=confidence,
        ),
        snippet=snippet,
    )


_ROOMY_BUDGET = ContextConfig(max_chars=10_000, max_files=100, max_graph_nodes=100)


def test_dedupes_same_file_and_location_keeping_higher_confidence() -> None:
    low = _item("a.py", entity="A", confidence=Confidence.HEURISTIC, snippet="low")
    high = _item("a.py", entity="A", confidence=Confidence.EXACT, snippet="high")

    result = build_context([low, high], [], budget=_ROOMY_BUDGET)

    assert len(result.evidence) == 1
    assert result.evidence[0]["confidence"] == "exact"
    assert result.evidence[0]["snippet"] == "high"


def test_distinct_locations_in_the_same_file_are_not_deduped() -> None:
    a = _item("a.py", entity="A", line_start=1)
    b = _item("a.py", entity="B", line_start=2)

    result = build_context([a, b], [], budget=_ROOMY_BUDGET)

    assert len(result.evidence) == 2


def test_exact_evidence_is_prioritized_over_heuristic_when_budget_is_tight() -> None:
    exact = _item("exact.py", confidence=Confidence.EXACT, snippet="e" * 10)
    heuristic = _item("heuristic.py", confidence=Confidence.HEURISTIC, snippet="h" * 10)

    budget = ContextConfig(max_chars=10, max_files=100, max_graph_nodes=100)
    result = build_context([heuristic, exact], [], budget=budget)

    assert len(result.evidence) == 1
    assert result.evidence[0]["path"] == "exact.py"
    assert result.truncated is True
    assert any("max_chars" in reason for reason in result.truncation_reasons)


def test_max_chars_caps_total_snippet_characters() -> None:
    items = [_item(f"f{i}.py", entity=f"e{i}", snippet="x" * 40) for i in range(5)]
    budget = ContextConfig(max_chars=100, max_files=100, max_graph_nodes=100)

    result = build_context(items, [], budget=budget)

    assert result.total_chars <= 100
    assert len(result.evidence) == 2  # floor(100 / 40)
    assert result.truncated is True


def test_max_files_caps_distinct_files_not_items_within_a_file() -> None:
    same_file = [_item("a.py", entity=f"e{i}", line_start=i, snippet="x") for i in range(3)]
    other_file = [_item("b.py", entity="other", line_start=1, snippet="x")]
    budget = ContextConfig(max_chars=10_000, max_files=1, max_graph_nodes=100)

    result = build_context(same_file + other_file, [], budget=budget)

    assert result.file_count == 1
    assert {row["path"] for row in result.evidence} == {"a.py"}
    assert len(result.evidence) == 3
    assert result.truncated is True
    assert any("max_files" in reason for reason in result.truncation_reasons)


def test_max_graph_nodes_caps_distinct_endpoints() -> None:
    paths = [
        GraphPath(source=f"A{i}", relationship="calls", target="Target", confidence="high")
        for i in range(5)
    ]
    budget = ContextConfig(max_chars=10_000, max_files=100, max_graph_nodes=3)

    result = build_context([], paths, budget=budget)

    assert result.graph_node_count <= 3
    assert len(result.graph_paths) < len(paths)
    assert result.truncated is True
    assert any("max_graph_nodes" in reason for reason in result.truncation_reasons)


def test_no_truncation_when_everything_fits() -> None:
    result = build_context([_item("a.py")], [], budget=_ROOMY_BUDGET)
    assert result.truncated is False
    assert result.truncation_reasons == []
