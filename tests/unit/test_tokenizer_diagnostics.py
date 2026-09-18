"""Exact Tokenizer plan, Phase 5: tokenizer identity + payload-scan
observability helpers (``ragmonk.tokenization.diagnostics``). Default
(offline) suite.
"""

from __future__ import annotations

from ragmonk.tokenization import diagnostics, model_identity


def test_tokenizer_identity_reports_pinned_values() -> None:
    identity = diagnostics.tokenizer_identity()
    assert identity["model_id"] == model_identity.EMBEDDING_MODEL_ID
    assert identity["revision"] == model_identity.TOKENIZER_REVISION
    assert identity["fingerprint"] == model_identity.tokenizer_fingerprint()
    assert identity["max_sequence_tokens"] == model_identity.MAX_SEQUENCE_TOKENS


def test_scan_payloads_empty_is_all_zero() -> None:
    scan = diagnostics.scan_payloads([])
    assert scan.scanned == 0
    assert scan.max_payload_tokens == 0
    assert scan.truncation_count == 0
    assert scan.limit == model_identity.MAX_SEQUENCE_TOKENS


def test_scan_payloads_counts_max_and_truncations() -> None:
    within = "the cat sat on the mat"
    # 300 single-token words -> 300 + 2 special = 302 > 256 -> a truncation.
    oversized = " ".join(["the"] * 300)
    scan = diagnostics.scan_payloads(["", within, oversized])
    assert scan.scanned == 2  # the empty string is skipped
    assert scan.truncation_count == 1
    assert scan.max_payload_tokens > model_identity.MAX_SEQUENCE_TOKENS


def test_scan_payloads_no_truncation_when_all_fit() -> None:
    scan = diagnostics.scan_payloads(["short one", "short two"])
    assert scan.scanned == 2
    assert scan.truncation_count == 0
    assert scan.max_payload_tokens <= model_identity.MAX_SEQUENCE_TOKENS
