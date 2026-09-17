"""Unit tests for the two small, documented heuristics ``impact``/
``explore`` share: the test-file naming-convention filter
(``retrieval/graph.py``'s ``is_test_file``) and the blast-radius bucketing
(``cli/impact.py``'s ``_blast_radius``).
"""

from __future__ import annotations

import pytest

from ragmonk.cli.impact import _blast_radius
from ragmonk.retrieval.graph import is_test_file


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_settlement.py",
        "services/settlement_test.py",
        "pkg/handlers_test.go",
        "SettlementServiceTest.cs",
        "SettlementServiceTests.cs",
        "com/acme/SettlementServiceTest.java",
        "src/settlement.test.ts",
        "src/settlement.test.js",
        "src/settlement.spec.tsx",
        r"D:\a\proj\proj\tests\test_settlement.py",  # Windows-style separators
        r"C:\repo\pkg\handlers_test.go",
    ],
)
def test_recognizes_common_test_naming_conventions(path: str) -> None:
    assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "services/settlement_service.py",
        "src/Contracts/BetSettled.cs",
        "docs/settlement-rules.md",
        "pkg/testament.py",  # must not false-positive on a name that merely contains "test"
        "pkg/attestation.go",
    ],
)
def test_does_not_flag_non_test_files(path: str) -> None:
    assert is_test_file(path) is False


@pytest.mark.parametrize("score", [0, 1, 2])
def test_blast_radius_low_boundary(score: int) -> None:
    assert _blast_radius(score) == "LOW"


@pytest.mark.parametrize("score", [3, 5, 7])
def test_blast_radius_medium_boundary(score: int) -> None:
    assert _blast_radius(score) == "MEDIUM"


@pytest.mark.parametrize("score", [8, 9, 100])
def test_blast_radius_high_boundary(score: int) -> None:
    assert _blast_radius(score) == "HIGH"
