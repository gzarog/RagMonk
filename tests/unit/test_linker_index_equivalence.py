"""The word-index accelerated matchers must produce exactly what the
original "every needle x every unit" regex scan produced, in the same
order. Compared on randomized text full of near-misses (dotted paths,
prefixes/suffixes, punctuation, non-ASCII identifiers).
"""

from __future__ import annotations

import random
import re

import pytest
from tests.unit.test_linker import _entity, _unit

from ragmonk.knowledge import linker

_WORDS = ["Dog", "bark", "dog", "Barker", "pkg", "models", "ümlaut", "x1", "_private", "run"]
_SEPARATORS = [" ", ".", ". ", "(", ")", "-", "_", ",", "\n", "::", "/", "`"]


def _reference(needle: str, units: list) -> list:  # type: ignore[type-arg]
    if not needle:
        return []
    pattern = re.compile(r"(?<![\w.])" + re.escape(needle) + r"(?![\w.])")
    return [u for u in units if pattern.search(u.text)]


def _random_text(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 25)):
        parts.append(rng.choice(_WORDS))
        parts.append(rng.choice(_SEPARATORS))
    return "".join(parts)


def _random_qualified(rng: random.Random) -> str:
    return ".".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4)))


@pytest.mark.parametrize("seed", range(20))
def test_indexed_matchers_equal_bruteforce(seed: int) -> None:
    rng = random.Random(seed)
    units = [_unit(f"u{i}", _random_text(rng)) for i in range(40)]
    entities = []
    for i in range(30):
        qualified = _random_qualified(rng)
        entities.append(_entity(f"e{i}", qualified.split(".")[-1], qualified))

    exact = linker.match_exact_identifier(entities, units)
    assert [(c.entity_id, c.section_id) for c in exact] == [
        (e.id, u.id) for e in entities for u in _reference(e.name, units)
    ]

    qualified = linker.match_qualified_identifier(entities, units)
    assert [(c.entity_id, c.section_id) for c in qualified] == [
        (e.id, u.id)
        for e in entities
        if "." in e.qualified_name
        for u in _reference(e.qualified_name, units)
    ]

    alias = linker.match_alias(entities, units)
    expected_alias = []
    for e in entities:
        segments = e.qualified_name.split(".")
        if len(segments) >= 3:
            expected_alias += [(e.id, u.id) for u in _reference(".".join(segments[-2:]), units)]
    assert [(c.entity_id, c.section_id) for c in alias] == expected_alias

    namespace = entities[0]
    filenames = ["Dog.py", "Dog", "run"]
    got = [(c.section_id, c.evidence) for c in linker.match_filename(namespace, filenames, units)]
    expected = []
    for u in units:
        for name in filenames:
            if _reference(name, [u]):
                expected.append((u.id, name))
                break
    assert got == expected


def test_shared_index_is_only_used_for_its_own_units() -> None:
    units_a = [_unit("a", "call bark here")]
    units_b = [_unit("b", "nothing")]
    index_a = linker.UnitIndex(units_a)
    dog = _entity("e1", "bark", "pkg.Dog.bark")
    # A mismatched index must be ignored, not silently reused.
    assert linker.match_exact_identifier([dog], units_b, index=index_a) == []
    assert len(linker.match_exact_identifier([dog], units_a, index=index_a)) == 1
