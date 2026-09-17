"""Single source of truth for RagMonk's embedding-model / tokenizer identity.

Every part of RagMonk that needs to know *which* model produces vectors,
*which* tokenizer decides chunk boundaries, or *what* input length the
model really accepts imports these constants from here -- never
re-declares them. ``retrieval/embedder.py`` re-exports ``EMBEDDING_MODEL_ID``
from this module so the embedder and the tokenizer can never drift onto
two different model identities (see the Exact Tokenizer plan, "one shared
model identity and startup validation").

The tokenizer assets themselves are bundled under ``assets/`` at the
pinned revision below and verified against ``TOKENIZER_ASSET_MANIFEST``
on load, so normal indexing never contacts Hugging Face.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# The embedding model. This is the *one* place the id is written; the
# embedder imports it from here.
EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

# Pinned Hugging Face commit the bundled tokenizer assets were taken from
# -- a fixed 40-char commit sha, never a floating branch/tag head, so the
# bundled bytes and this identity string always describe the same assets.
TOKENIZER_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

# The model's *effective* maximum input length in tokens, including the
# two special tokens (``[CLS]``/``[SEP]``). all-MiniLM-L6-v2 exposes
# ``max_position_embeddings`` / ``model_max_length`` of 512, but the
# sentence-transformers configuration this project's embedder mirrors
# (``sentence_bert_config.json``'s ``max_seq_length``) truncates every
# input to 256 -- so 256, not 512, is the real ceiling a chunk must fit
# under to avoid silent truncation at embedding time.
MAX_SEQUENCE_TOKENS = 256

# Directory (relative to this file) holding the bundled tokenizer assets.
TOKENIZER_ASSET_SUBDIR = "assets/all-MiniLM-L6-v2"

# SHA-256 of every bundled asset file. Loading verifies each file against
# this map and fails loudly on any mismatch or missing file -- a modified
# or truncated asset must never be used silently. Regenerate with
# ``scripts/refresh_tokenizer_assets.py`` if the pinned revision changes.
TOKENIZER_ASSET_MANIFEST: dict[str, str] = {
    "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
    "sentence_bert_config.json": "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7",
    "special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
    "tokenizer_config.json": "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b",
    "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
}

# The single tokenizer file the runtime actually loads; the rest are
# bundled for identity/verification and future transformers-based loading.
PRIMARY_TOKENIZER_FILE = "tokenizer.json"


def tokenizer_asset_dir() -> Path:
    """Absolute path to the bundled tokenizer asset directory."""
    return Path(__file__).resolve().parent / TOKENIZER_ASSET_SUBDIR


def tokenizer_fingerprint() -> str:
    """Deterministic ``sha256:<hex>`` fingerprint of the pinned asset set.

    Derived purely from ``TOKENIZER_ASSET_MANIFEST`` (sorted
    ``name:sha256`` lines), so it changes if and only if the pinned asset
    bytes change. Stamped into the index identity (Phase 4) and reported
    by diagnostics (Phase 5).
    """
    payload = "\n".join(
        f"{name}:{digest}" for name, digest in sorted(TOKENIZER_ASSET_MANIFEST.items())
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
