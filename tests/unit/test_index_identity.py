"""Exact Tokenizer plan, Phase 4: the document index-derivation identity
(``document_version_stamp``) embeds the exact tokenizer's identity, so a
tokenizer change reprocesses affected files through the existing graceful
version-drift path (``indexing/incremental.decide_reprocessing``) -- no
hard index rejection.
"""

from __future__ import annotations

import pytest

from ragmonk.documents import pipeline
from ragmonk.tokenization import model_identity


def test_version_stamp_embeds_tokenizer_identity() -> None:
    stamp = pipeline.document_version_stamp()
    assert stamp.chunker_version is not None
    # Revision, asset fingerprint and max sequence length all fold in --
    # each affects chunk boundaries.
    assert model_identity.TOKENIZER_REVISION in stamp.chunker_version
    assert model_identity.tokenizer_fingerprint() in stamp.chunker_version
    assert f"max{model_identity.MAX_SEQUENCE_TOKENS}" in stamp.chunker_version
    # The embedding model identity is still its own axis.
    assert stamp.embedding_model_id == model_identity.EMBEDDING_MODEL_ID


def test_tokenizer_revision_change_changes_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    before = pipeline.document_version_stamp().chunker_version
    monkeypatch.setattr(model_identity, "TOKENIZER_REVISION", "deadbeef" * 5)
    after = pipeline.document_version_stamp().chunker_version
    assert before != after
    assert "deadbeef" in (after or "")


def test_max_sequence_length_change_changes_the_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    before = pipeline.document_version_stamp().chunker_version
    monkeypatch.setattr(model_identity, "MAX_SEQUENCE_TOKENS", 128)
    after = pipeline.document_version_stamp().chunker_version
    assert before != after
    assert "max128" in (after or "")
