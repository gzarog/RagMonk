"""Exact token counting and token-budget splitting for chunk boundaries.

Exact Tokenizer plan, Phase 2: this module used to estimate token counts
with a regex + ~4-characters-per-token heuristic. It now delegates to the
real, pinned tokenizer of the embedding model
(``ragmonk.tokenization.model_tokenizer.get_model_tokenizer``) so every
count here is exactly what the embedder will see -- no more silent
over/under-counting of Greek, code, URLs, or punctuation-heavy text. The
tokenizer loads bundled, offline assets (no network), lazily on first use.

``count_tokens`` reports *body* tokens (no model special tokens); the
chunker adds the special-token and contextual-header cost separately when
it budgets the full embedding payload (see ``documents/chunker.py``). The
WordPiece tokenizer pre-splits on whitespace, so counts are additive
across whitespace-joined segments and inter-piece separators
(spaces/newlines) cost zero tokens -- the budgeting in ``chunker.py``
relies on both properties.
"""

from __future__ import annotations

import re

from ragmonk.tokenization.model_tokenizer import get_model_tokenizer

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def count_tokens(text: str) -> int:
    """Exact body-token count of ``text`` (no model special tokens)."""
    if not text:
        return 0
    return get_model_tokenizer().count(text, add_special_tokens=False)


def split_sentences(text: str) -> list[str]:
    """Splits on sentence-ending punctuation followed by whitespace.

    Never returns an empty list for non-empty input -- a text with no
    recognizable sentence boundary (no ``.``/``!``/``?``) comes back as
    one "sentence" so callers can fall through to word-level splitting.
    """
    stripped = text.strip()
    if not stripped:
        return []
    return [s for s in _SENTENCE_SPLIT_RE.split(stripped) if s]


def split_by_token_budget(text: str, max_tokens: int) -> list[str]:
    """Splits ``text`` into pieces that each fit within ``max_tokens``
    exact body tokens, preferring sentence boundaries, then word
    boundaries, and finally the tokenizer's own sub-word offsets for a
    single word that alone exceeds the budget -- never a raw mid-character
    cut. Adjacent small sentences are re-packed together up to
    ``max_tokens`` so an over-long paragraph doesn't degrade into one
    chunk per sentence when several short ones would still fit together.

    Returns ``[text]`` unchanged if it already fits; never returns an
    empty list for non-empty input.
    """
    if not text:
        return []
    tokenizer = get_model_tokenizer()
    if tokenizer.count(text, add_special_tokens=False) <= max_tokens:
        return [text]

    pieces: list[str] = []
    for sentence in split_sentences(text):
        if tokenizer.count(sentence, add_special_tokens=False) <= max_tokens:
            pieces.append(sentence)
            continue
        # Sentence alone exceeds the budget: pack its words (WordPiece is
        # additive across whitespace, so summed word counts are exact),
        # and sub-word-split any single word that itself overflows.
        buf: list[str] = []
        buf_tokens = 0
        for word in sentence.split():
            word_tokens = tokenizer.count(word, add_special_tokens=False)
            if word_tokens > max_tokens:
                if buf:
                    pieces.append(" ".join(buf))
                    buf, buf_tokens = [], 0
                pieces.extend(tokenizer.split(word, max_tokens))
                continue
            if buf and buf_tokens + word_tokens > max_tokens:
                pieces.append(" ".join(buf))
                buf, buf_tokens = [], 0
            buf.append(word)
            buf_tokens += word_tokens
        if buf:
            pieces.append(" ".join(buf))

    return _repack(pieces, max_tokens)


def _repack(pieces: list[str], max_tokens: int) -> list[str]:
    tokenizer = get_model_tokenizer()
    packed: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for piece in pieces:
        piece_tokens = tokenizer.count(piece, add_special_tokens=False)
        if buf and buf_tokens + piece_tokens > max_tokens:
            packed.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(piece)
        buf_tokens += piece_tokens
    if buf:
        packed.append(" ".join(buf))
    return packed
