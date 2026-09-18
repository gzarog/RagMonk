"""Exact, offline tokenizer service (Exact Tokenizer plan, Phase 1).

These tests run in the DEFAULT suite: the tokenizer loads bundled,
pinned assets through the ``tokenizers`` library with no network access
and no model-weight download, so -- unlike the ``embedding_model``-marked
embedder tests -- they need no marker. The exact counts below are
snapshots of the pinned ``all-MiniLM-L6-v2`` WordPiece tokenizer; they
change only if the pinned revision changes.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from ragmonk.tokenization import model_identity
from ragmonk.tokenization.model_tokenizer import (
    ModelTokenizer,
    TokenizerAssetError,
    _reset_for_tests,
    get_model_tokenizer,
)


@pytest.fixture()
def tokenizer() -> ModelTokenizer:
    _reset_for_tests()
    tok = get_model_tokenizer()
    yield tok
    _reset_for_tests()


def test_identity_constants_are_pinned(tokenizer: ModelTokenizer) -> None:
    assert tokenizer.model_id == "sentence-transformers/all-MiniLM-L6-v2"
    assert tokenizer.model_revision == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    assert tokenizer.max_sequence_tokens == 256
    assert tokenizer.fingerprint.startswith("sha256:")
    assert tokenizer.fingerprint == model_identity.tokenizer_fingerprint()


@pytest.mark.parametrize(
    ("text", "with_special", "without_special"),
    [
        ("Hello world", 4, 2),
        ("Καλημέρα κόσμε", 15, 13),  # Greek
        ("public async Task<int> GetCountAsync() => await _repo.CountAsync();", 32, 30),
        ("https://example.com/path/to/resource?id=42&ref=abc-def-123", 27, 25),
        ("550e8400-e29b-41d4-a716-446655440000", 25, 23),
    ],
)
def test_exact_counts_match_pinned_tokenizer(
    tokenizer: ModelTokenizer, text: str, with_special: int, without_special: int
) -> None:
    assert tokenizer.count(text) == with_special
    assert tokenizer.count(text, add_special_tokens=True) == with_special
    assert tokenizer.count(text, add_special_tokens=False) == without_special


def test_special_tokens_add_exactly_two(tokenizer: ModelTokenizer) -> None:
    # The model wraps every input in exactly ``[CLS] ... [SEP]``.
    for text in ("Hello world", "Καλημέρα", "a"):
        assert tokenizer.count(text) == tokenizer.count(text, add_special_tokens=False) + 2


def test_empty_string_counts(tokenizer: ModelTokenizer) -> None:
    assert tokenizer.count("", add_special_tokens=False) == 0
    # An empty body still costs the two special tokens.
    assert tokenizer.count("") == 2


def test_count_matches_independent_tokenizers_load(tokenizer: ModelTokenizer) -> None:
    """Parity against a fresh, independent load of the same bundled file."""
    from tokenizers import Tokenizer

    asset = model_identity.tokenizer_asset_dir() / model_identity.PRIMARY_TOKENIZER_FILE
    reference = Tokenizer.from_file(str(asset))
    reference.no_padding()
    reference.no_truncation()
    for text in ("Hello world", "Καλημέρα κόσμε", "x" * 500):
        assert tokenizer.count(text, add_special_tokens=False) == len(
            reference.encode(text, add_special_tokens=False).ids
        )


def test_split_returns_input_unchanged_when_it_fits(tokenizer: ModelTokenizer) -> None:
    assert tokenizer.split("Hello world", budget=50) == ["Hello world"]


def test_split_empty_is_empty(tokenizer: ModelTokenizer) -> None:
    assert tokenizer.split("", budget=10) == []


def test_split_rejects_nonpositive_budget(tokenizer: ModelTokenizer) -> None:
    with pytest.raises(ValueError):
        tokenizer.split("anything", budget=0)
    with pytest.raises(ValueError):
        tokenizer.split("anything", budget=-3)


def test_split_pieces_each_fit_budget_and_are_substrings(tokenizer: ModelTokenizer) -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 40
    budget = 20
    pieces = tokenizer.split(text, budget=budget)
    assert len(pieces) > 1
    for piece in pieces:
        assert piece  # never empty
        assert piece in text  # preserves original spans, no mid-char cut
        assert tokenizer.count(piece, add_special_tokens=False) <= budget


def test_split_handles_single_oversized_word(tokenizer: ModelTokenizer) -> None:
    # A single long word that WordPieces into many sub-word tokens (17
    # here) still splits at sub-word offsets, never mid-character.
    word = "pneumonoultramicroscopicsilicovolcanoconiosis"
    assert tokenizer.count(word, add_special_tokens=False) > 8
    pieces = tokenizer.split(word, budget=8)
    assert len(pieces) > 1
    assert "".join(pieces) == word  # sub-word cuts of a spaceless word are lossless
    for piece in pieces:
        assert tokenizer.count(piece, add_special_tokens=False) <= 8


def test_instance_is_cached_per_process() -> None:
    _reset_for_tests()
    first = get_model_tokenizer()
    second = get_model_tokenizer()
    assert first is second
    _reset_for_tests()


def test_loading_performs_no_network_import() -> None:
    # Loading the bundled tokenizer must not reach the Hub. Proven in a
    # fresh subprocess (the shared test process already imports
    # huggingface_hub via other suites, so an in-process sys.modules check
    # would be polluted): after loading and using the tokenizer, neither
    # the Hub client nor an HTTP client library may have been imported.
    script = (
        "import sys\n"
        "from ragmonk.tokenization.model_tokenizer import get_model_tokenizer\n"
        "tok = get_model_tokenizer()\n"
        "tok.count('warm the tokenizer')\n"
        "tok.split('a longer piece of text to exercise splitting ' * 5, 8)\n"
        "network = [m for m in ('huggingface_hub', 'requests', 'urllib3', 'httpx') "
        "if m in sys.modules]\n"
        "print('NETWORK_MODULES:' + ','.join(network))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("NETWORK_MODULES:")),
        None,
    )
    assert line is not None, f"no marker in output; stderr:\n{result.stderr}"
    loaded = line[len("NETWORK_MODULES:") :]
    assert loaded == "", f"loading the bundled tokenizer imported network modules: {loaded}"


def test_missing_asset_dir_raises_actionable_error(monkeypatch, tmp_path) -> None:
    _reset_for_tests()
    monkeypatch.setattr(model_identity, "tokenizer_asset_dir", lambda: tmp_path / "nope")
    with pytest.raises(TokenizerAssetError, match="missing"):
        ModelTokenizer()
    _reset_for_tests()


def test_corrupt_asset_fails_integrity_check(monkeypatch, tmp_path) -> None:
    _reset_for_tests()
    # Copy the real assets, then corrupt one, and point the loader at them.
    real_dir = model_identity.tokenizer_asset_dir()
    for name in model_identity.TOKENIZER_ASSET_MANIFEST:
        (tmp_path / name).write_bytes((real_dir / name).read_bytes())
    (tmp_path / "tokenizer.json").write_bytes(b"{corrupted}")
    monkeypatch.setattr(model_identity, "tokenizer_asset_dir", lambda: tmp_path)
    with pytest.raises(TokenizerAssetError, match="integrity"):
        ModelTokenizer()
    _reset_for_tests()


def test_fingerprint_is_deterministic_and_manifest_derived() -> None:
    assert model_identity.tokenizer_fingerprint() == model_identity.tokenizer_fingerprint()
    assert model_identity.tokenizer_fingerprint().startswith("sha256:")


def test_embedder_shares_the_single_model_identity() -> None:
    from ragmonk.retrieval import embedder

    assert embedder.EMBEDDING_MODEL_ID == model_identity.EMBEDDING_MODEL_ID
