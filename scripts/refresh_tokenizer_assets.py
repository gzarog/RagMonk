#!/usr/bin/env python3
"""Re-download and re-hash the bundled embedding-model tokenizer assets.

Maintainer tool, not run at install/index time. Downloads the pinned
tokenizer files for ``EMBEDDING_MODEL_ID`` at ``TOKENIZER_REVISION`` from
the Hugging Face Hub into the bundled asset directory and prints a fresh
``TOKENIZER_ASSET_MANIFEST`` block to paste into
``ragmonk.tokenization.model_identity``.

Run this only when intentionally bumping the pinned revision:

    python scripts/refresh_tokenizer_assets.py

Requires network access and ``huggingface_hub`` (a dev-only requirement;
neither is needed for normal indexing, which reads the bundled files
offline). This deliberately lives outside the runtime package so the
runtime never depends on ``huggingface_hub``.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from ragmonk.tokenization import model_identity

# The files RagMonk bundles. tokenizer.json is the only one the runtime
# loads; the rest pin identity/config for verification and future
# transformers-based loading.
ASSET_FILES = [
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "config.json",
    "sentence_bert_config.json",
]


def main() -> int:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print(
            "huggingface_hub is required to refresh assets: pip install huggingface_hub",
            file=sys.stderr,
        )
        return 1

    dest = model_identity.tokenizer_asset_dir()
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for name in ASSET_FILES:
        downloaded = hf_hub_download(
            model_identity.EMBEDDING_MODEL_ID,
            name,
            revision=model_identity.TOKENIZER_REVISION,
        )
        data = Path(downloaded).read_bytes()
        (dest / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()

    print(f"Wrote {len(manifest)} assets to {dest}\n")
    print("TOKENIZER_ASSET_MANIFEST: dict[str, str] = {")
    for name, digest in sorted(manifest.items()):
        print(f'    "{name}": "{digest}",')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
