"""Observability helpers for the exact tokenizer (Exact Tokenizer plan,
Phase 5).

``tokenizer_identity`` is cheap -- it reads the pinned identity constants
and never loads the tokenizer -- so ``status``/``search --explain`` can
report which tokenizer the active index is tied to without paying a model
load. ``scan_payloads`` does load the tokenizer to measure the exact size
of stored embedding payloads, proving the no-silent-truncation invariant
holds for the active index (``ragmonk doctor``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ragmonk.tokenization.model_identity import (
    EMBEDDING_MODEL_ID,
    MAX_SEQUENCE_TOKENS,
    TOKENIZER_REVISION,
    tokenizer_fingerprint,
)
from ragmonk.tokenization.model_tokenizer import get_model_tokenizer


def tokenizer_identity() -> dict[str, Any]:
    """The pinned tokenizer/model identity, cheap to compute (no load)."""
    return {
        "model_id": EMBEDDING_MODEL_ID,
        "revision": TOKENIZER_REVISION,
        "fingerprint": tokenizer_fingerprint(),
        "max_sequence_tokens": MAX_SEQUENCE_TOKENS,
    }


@dataclass(frozen=True)
class PayloadScan:
    """Result of measuring stored embedding payloads against the model's
    real input limit. ``truncation_count`` is the number of payloads that
    would be silently truncated at embedding time (must remain zero for an
    index built by the exact tokenizer).
    """

    scanned: int
    max_payload_tokens: int
    truncation_count: int
    limit: int


def scan_payloads(texts: Iterable[str], *, limit: int | None = None) -> PayloadScan:
    """Counts each non-empty payload's exact token length (with the model's
    special tokens) and reports the maximum and how many exceed ``limit``
    (default: the model's ``MAX_SEQUENCE_TOKENS``, the point past which the
    embedder truncates).
    """
    effective_limit = MAX_SEQUENCE_TOKENS if limit is None else limit
    tokenizer = get_model_tokenizer()
    scanned = 0
    max_payload = 0
    truncated = 0
    for text in texts:
        if not text:
            continue
        size = tokenizer.count(text, add_special_tokens=True)
        scanned += 1
        if size > max_payload:
            max_payload = size
        if size > effective_limit:
            truncated += 1
    return PayloadScan(
        scanned=scanned,
        max_payload_tokens=max_payload,
        truncation_count=truncated,
        limit=effective_limit,
    )
