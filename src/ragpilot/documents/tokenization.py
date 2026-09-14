"""Approximate, dependency-free token counting for chunk-boundary decisions.

``chunker.py`` needs real token counts to honor ``max_tokens``/
``min_tokens``/``overlap_tokens`` -- a flat ``len(text)`` proxy (the
previous ``DEFAULT_MAX_CHUNK_CHARS`` behavior) systematically over-packs
punctuation-heavy text and under-packs long-word text relative to what an
actual subword tokenizer would count.

The obvious "just reuse the embedding model's tokenizer" choice
(``retrieval/embedder.py``'s ``sentence-transformers/all-MiniLM-L6-v2``
WordPiece tokenizer, loaded via ``transformers.AutoTokenizer.
from_pretrained``) was deliberately rejected for two independent reasons,
either one alone sufficient:

1. **Network/offline-test contract.** Loading it downloads and caches
   model files from Hugging Face on first use -- the exact real-network
   dependency ``CONTRIBUTING.md``'s ``embedding_model`` marker exists to
   keep *out* of the default test suite (see ``retrieval/embedder.py``'s
   own docstring: "Everything else... is tested with precomputed/fake
   vectors... runs in the default suite"). This phase's own test list
   (long-paragraph splitting, exact token-limit enforcement, etc.) has to
   run in that same default, offline, deterministic suite.
2. **Degrade-gracefully guarantee.** Chunking has to keep working when
   the embedding model is entirely unavailable (``EmbeddingModelUnavailable
   Error``, FTS-only indexing with ``search.semantic`` off) -- making
   chunk *boundaries* themselves depend on that model being loadable
   would regress that guarantee for every document, not just semantic
   search.

So this module is a small, self-contained word/punctuation tokenizer
instead: it pre-tokenizes the same way most subword tokenizers' own
pre-tokenizer step does (runs of word characters vs. individual
punctuation/symbol characters), then estimates each word-piece's subword
count from its length -- ~4 characters per token is the commonly cited
average for English BPE/WordPiece vocabularies, this project's own
embedding model's included. It will not match ``AutoTokenizer(...)
.encode()`` exactly, but it tracks real token-shaped units (words,
numbers, punctuation) rather than a raw character count, is fully
deterministic, adds no dependency, and never touches the network.
"""

from __future__ import annotations

import re

# A run of "word" characters (letters/digits/underscore, Unicode-aware via
# \w) is one piece; any other non-space character (punctuation, symbols,
# CJK handled per-character by \w already) is its own piece -- mirrors the
# split most BPE/WordPiece pre-tokenizers apply before subword merging.
_PIECE_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)

# Commonly cited average English subword length (OpenAI's own tiktoken
# guidance, and in the same range as this project's embedding model's
# WordPiece vocabulary) -- used to turn a word-piece's character length
# into an estimated subword-token count.
_CHARS_PER_TOKEN = 4

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _piece_token_count(piece: str) -> int:
    if piece[0].isalnum() or piece[0] == "_":
        return -(-len(piece) // _CHARS_PER_TOKEN)  # ceil division, min 1 for non-empty piece
    return 1  # a lone punctuation/symbol character is always exactly one token


def count_tokens(text: str) -> int:
    """Estimated subword-token count of ``text`` -- see module docstring."""
    if not text:
        return 0
    return sum(_piece_token_count(p) for p in _PIECE_RE.findall(text))


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
    """Splits ``text`` into pieces that each fit within ``max_tokens``,
    preferring sentence boundaries and falling back to word boundaries for
    any single sentence that alone exceeds the budget -- never a raw
    mid-word character cut. Adjacent small sentences are re-packed
    together up to ``max_tokens`` so an over-long paragraph doesn't
    degrade into one chunk per sentence when several short ones would
    still fit the budget together.

    Returns ``[text]`` unchanged if it already fits; never returns an
    empty list for non-empty input.
    """
    if not text:
        return []
    if count_tokens(text) <= max_tokens:
        return [text]

    pieces: list[str] = []
    for sentence in split_sentences(text):
        if count_tokens(sentence) <= max_tokens:
            pieces.append(sentence)
            continue
        words = sentence.split()
        buf: list[str] = []
        buf_tokens = 0
        for word in words:
            word_tokens = count_tokens(word)
            if buf and buf_tokens + word_tokens > max_tokens:
                pieces.append(" ".join(buf))
                buf, buf_tokens = [], 0
            buf.append(word)
            buf_tokens += word_tokens
        if buf:
            pieces.append(" ".join(buf))

    return _repack(pieces, max_tokens)


def _repack(pieces: list[str], max_tokens: int) -> list[str]:
    packed: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for piece in pieces:
        piece_tokens = count_tokens(piece)
        if buf and buf_tokens + piece_tokens > max_tokens:
            packed.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(piece)
        buf_tokens += piece_tokens
    if buf:
        packed.append(" ".join(buf))
    return packed
