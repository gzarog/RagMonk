"""Exact Tokenizer plan, Phase 2: ``documents/tokenization.py`` now
delegates to the real embedding tokenizer, so these assert exact
tokenizer behavior/properties rather than the old ~4-chars-per-token
estimator's specific numbers (which were deliberately removed)."""

from __future__ import annotations

from ragmonk.documents.tokenization import count_tokens, split_by_token_budget, split_sentences


def test_count_tokens_empty_string_is_zero() -> None:
    assert count_tokens("") == 0


def test_count_tokens_counts_a_common_word_as_one_token() -> None:
    assert count_tokens("word") == 1


def test_count_tokens_counts_punctuation_as_its_own_token() -> None:
    assert count_tokens("hi!") == count_tokens("hi") + 1


def test_count_tokens_grows_with_word_length() -> None:
    # The exact WordPiece tokenizer breaks a long unknown word into more
    # sub-word tokens, so the count is non-decreasing in length and a long
    # word costs more than a single token (kept under WordPiece's
    # 100-char-per-word cap, past which a word collapses to one [UNK]).
    assert count_tokens("a") == 1
    counts = [count_tokens("a" * n) for n in (1, 4, 8, 16, 32)]
    assert counts == sorted(counts)
    assert counts[-1] > counts[0]


def test_count_tokens_is_not_a_flat_character_proxy() -> None:
    # Same character count, very different real word-shape -- a raw
    # len(text)/4 proxy would score these identically; the exact
    # tokenizer does not.
    many_short_words = "the cat dog run " * 4  # lots of punctuation-free short words
    one_long_word = "a" * len(many_short_words)
    assert count_tokens(many_short_words) != count_tokens(one_long_word)


def test_split_sentences_splits_on_terminal_punctuation() -> None:
    text = "First sentence. Second sentence! Third one?"
    assert split_sentences(text) == ["First sentence.", "Second sentence!", "Third one?"]


def test_split_sentences_empty_text_returns_empty_list() -> None:
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_split_by_token_budget_returns_text_unchanged_when_it_fits() -> None:
    text = "A short paragraph that easily fits."
    assert split_by_token_budget(text, max_tokens=100) == [text]


def test_split_by_token_budget_never_exceeds_the_budget() -> None:
    text = " ".join(f"word{i} is here in sentence {i}." for i in range(50))
    pieces = split_by_token_budget(text, max_tokens=12)
    assert len(pieces) > 1
    for piece in pieces:
        assert count_tokens(piece) <= 12


def test_split_by_token_budget_falls_back_to_word_splitting_for_one_huge_sentence() -> None:
    # One sentence, no internal punctuation to split on -- must still
    # respect the budget by falling back to word boundaries.
    text = " ".join(f"word{i}" for i in range(60)) + "."
    pieces = split_by_token_budget(text, max_tokens=10)
    assert len(pieces) > 1
    for piece in pieces:
        assert count_tokens(piece) <= 10
    # Every original word survives somewhere, in order.
    assert " ".join(pieces).split() == text.split()


def test_split_by_token_budget_preserves_word_order() -> None:
    text = " ".join(f"item{i}" for i in range(40))
    pieces = split_by_token_budget(text, max_tokens=8)
    assert " ".join(pieces).split() == text.split()
