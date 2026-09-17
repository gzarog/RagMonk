"""Exact, offline tokenization for RagMonk's embedding model.

This package owns the *one* source of truth for the embedding-model
identity (``model_identity``) and the pinned, bundled, network-free
tokenizer service (``model_tokenizer``) used to budget chunks against the
model's real input limit. See ``model_tokenizer.ModelTokenizer`` and the
RagMonk Exact Tokenizer Implementation Plan.
"""

from __future__ import annotations

from ragmonk.tokenization.model_identity import (
    EMBEDDING_MODEL_ID,
    MAX_SEQUENCE_TOKENS,
    TOKENIZER_ASSET_MANIFEST,
    TOKENIZER_REVISION,
    tokenizer_fingerprint,
)
from ragmonk.tokenization.model_tokenizer import (
    ModelTokenizer,
    TokenizerAssetError,
    get_model_tokenizer,
)

__all__ = [
    "EMBEDDING_MODEL_ID",
    "MAX_SEQUENCE_TOKENS",
    "TOKENIZER_ASSET_MANIFEST",
    "TOKENIZER_REVISION",
    "ModelTokenizer",
    "TokenizerAssetError",
    "get_model_tokenizer",
    "tokenizer_fingerprint",
]
