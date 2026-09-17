from __future__ import annotations

from ragmonk.core.models import Confidence
from ragmonk.knowledge import confidence


def test_rank_orders_exact_above_high_above_medium_above_heuristic() -> None:
    assert (
        confidence.rank(Confidence.EXACT)
        > confidence.rank(Confidence.HIGH)
        > confidence.rank(Confidence.MEDIUM)
        > confidence.rank(Confidence.HEURISTIC)
    )


def test_at_least_true_for_equal_or_stronger() -> None:
    assert confidence.at_least(Confidence.EXACT, Confidence.HIGH)
    assert confidence.at_least(Confidence.HIGH, Confidence.HIGH)


def test_at_least_false_for_weaker() -> None:
    assert not confidence.at_least(Confidence.MEDIUM, Confidence.HIGH)
    assert not confidence.at_least(Confidence.HEURISTIC, Confidence.MEDIUM)


def test_highest_picks_the_most_trusted_tier() -> None:
    assert (
        confidence.highest([Confidence.MEDIUM, Confidence.HEURISTIC, Confidence.HIGH])
        is Confidence.HIGH
    )


def test_highest_of_empty_is_none() -> None:
    assert confidence.highest([]) is None
