"""Exact, lazy, offline tokenizer service for the pinned embedding model.

This is the application-owned tokenizer interface the RagMonk chunker
budgets against (Exact Tokenizer plan, Phase 1). It loads the bundled,
pinned ``tokenizer.json`` for ``EMBEDDING_MODEL_ID`` through the Rust
``tokenizers`` library -- ``Tokenizer.from_file`` reads a local file and
never touches the network, satisfying the plan's ``local_files_only``
contract without importing ``transformers``/``torch`` at all.

Design guarantees:

* **Lazy.** Nothing loads until the first ``get_model_tokenizer()`` call
  (the first document that needs chunking); ``ragmonk --help``/``version``
  and other non-indexing commands never import ``tokenizers`` through
  this module.
* **Offline.** Only bundled assets are read. No Hugging Face Hub call is
  ever made.
* **Verified.** Every bundled asset is checked against
  ``TOKENIZER_ASSET_MANIFEST`` (SHA-256) before use; a missing or
  modified file raises :class:`TokenizerAssetError` with an actionable
  message. There is deliberately no fallback to the old approximate
  estimator.
* **Cached.** Exactly one :class:`ModelTokenizer` instance per process.
"""

from __future__ import annotations

import hashlib
import threading

from ragmonk.tokenization import model_identity
from ragmonk.tokenization.model_identity import (
    EMBEDDING_MODEL_ID,
    MAX_SEQUENCE_TOKENS,
    PRIMARY_TOKENIZER_FILE,
    TOKENIZER_ASSET_MANIFEST,
    TOKENIZER_REVISION,
    tokenizer_fingerprint,
)


class TokenizerAssetError(RuntimeError):
    """The bundled tokenizer could not be loaded or verified.

    Raised when the ``tokenizers`` runtime is missing, a bundled asset
    file is absent, or an asset's bytes do not match the pinned
    ``TOKENIZER_ASSET_MANIFEST`` hash. A specific, catchable type carrying
    a concrete remediation hint -- never a bare ``ImportError`` or a
    library-internal exception -- and never silently downgraded to
    approximate counting.
    """


def _verify_assets() -> None:
    """Checks every manifest-listed asset exists and matches its hash."""
    asset_dir = model_identity.tokenizer_asset_dir()
    for name, expected in sorted(TOKENIZER_ASSET_MANIFEST.items()):
        path = asset_dir / name
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise TokenizerAssetError(
                f"bundled tokenizer asset {name!r} is missing at {path} -- "
                f"the RagMonk installation is incomplete or corrupt; reinstall RagMonk"
            ) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise TokenizerAssetError(
                f"bundled tokenizer asset {name!r} at {path} failed integrity "
                f"verification (expected sha256 {expected}, got {actual}) -- "
                f"the file has been modified or corrupted; reinstall RagMonk"
            )


class ModelTokenizer:
    """Exact token counting and token-aware splitting for one model.

    Instances are created via :func:`get_model_tokenizer`, which caches a
    single instance per process. Construction verifies and loads the
    bundled assets, so a successfully constructed instance is always
    backed by the pinned, integrity-checked tokenizer.
    """

    model_id: str
    model_revision: str
    fingerprint: str
    max_sequence_tokens: int

    def __init__(self) -> None:
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise TokenizerAssetError(
                "the 'tokenizers' runtime required for exact token counting is not "
                "installed -- reinstall RagMonk (it is a direct dependency)"
            ) from exc

        _verify_assets()
        asset_path = model_identity.tokenizer_asset_dir() / PRIMARY_TOKENIZER_FILE
        try:
            tokenizer = Tokenizer.from_file(str(asset_path))
        except Exception as exc:  # noqa: BLE001 - normalize any loader error
            raise TokenizerAssetError(
                f"failed to load bundled tokenizer from {asset_path}: {exc}"
            ) from exc

        # The bundled ``tokenizer.json`` carries padding + truncation
        # configuration (the model is served at a fixed 256-token width);
        # both must be disabled here so ``count`` reflects the *true*
        # token length of the input rather than a padded/truncated 256.
        tokenizer.no_padding()
        tokenizer.no_truncation()

        self._tokenizer = tokenizer
        self.model_id = EMBEDDING_MODEL_ID
        self.model_revision = TOKENIZER_REVISION
        self.fingerprint = tokenizer_fingerprint()
        self.max_sequence_tokens = MAX_SEQUENCE_TOKENS

    def count(self, text: str, *, add_special_tokens: bool = True) -> int:
        """Exact number of tokens the model sees for ``text``.

        With ``add_special_tokens=True`` (the default) the count includes
        the model's ``[CLS]``/``[SEP]`` markers -- i.e. the real length of
        the sequence fed to the embedder, which is what a payload must fit
        under ``max_sequence_tokens``.
        """
        # An empty body still costs the special tokens when they are
        # requested (``[CLS][SEP]`` -> 2), and nothing otherwise.
        if not text and not add_special_tokens:
            return 0
        return len(self._tokenizer.encode(text, add_special_tokens=add_special_tokens).ids)

    def split(self, text: str, budget: int) -> list[str]:
        """Splits ``text`` into pieces that each re-encode within ``budget``.

        Cuts at exact token (sub-word) boundaries using the tokenizer's
        own offsets -- never mid-character -- and preserves the original
        substrings (each returned piece is a slice of ``text``). ``budget``
        counts body tokens only (``add_special_tokens=False``); the caller
        reserves room for special tokens and the contextual header.

        The guarantee is on the *re-encoded* piece: for each returned
        ``piece``, ``count(piece, add_special_tokens=False) <= budget``.
        This matters because a slice starting mid-word loses its ``##``
        continuation context and re-tokenizes into slightly more tokens
        than it occupied in the original stream, so a naive offset cut at
        exactly ``budget`` tokens can overshoot; the window is shrunk until
        the slice actually fits. The one unavoidable exception is a single
        source token whose own characters re-encode above ``budget`` (only
        possible for a pathologically small budget) -- progress always
        advances by at least one token so ``split`` still terminates.

        Returns ``[text]`` unchanged when it already fits and ``[]`` for
        empty input.
        """
        if budget <= 0:
            raise ValueError(f"split budget must be positive, got {budget}")
        if not text:
            return []
        encoding = self._tokenizer.encode(text, add_special_tokens=False)
        n = len(encoding.ids)
        if n <= budget:
            return [text]

        offsets = encoding.offsets
        pieces: list[str] = []
        start_tok = 0
        while start_tok < n:
            end_tok = min(start_tok + budget, n)
            char_start = offsets[start_tok][0]
            # Shrink the window until the re-encoded slice fits the budget,
            # but never below a single source token (guarantees progress).
            while end_tok > start_tok + 1:
                candidate = text[char_start : offsets[end_tok - 1][1]]
                if self.count(candidate, add_special_tokens=False) <= budget:
                    break
                end_tok -= 1
            piece = text[char_start : offsets[end_tok - 1][1]]
            if piece:
                pieces.append(piece)
            start_tok = end_tok
        return pieces


_lock = threading.Lock()
_instance: ModelTokenizer | None = None


def get_model_tokenizer() -> ModelTokenizer:
    """Returns the process-wide :class:`ModelTokenizer`, loading it once.

    Thread-safe and lazy: the first caller pays the (small) load cost and
    every later caller reuses the same object. Raises
    :class:`TokenizerAssetError` if the bundled assets cannot be loaded or
    verified.
    """
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is None:
            _instance = ModelTokenizer()
    return _instance


def _reset_for_tests() -> None:
    """Clears the cached instance. Test-only helper."""
    global _instance
    with _lock:
        _instance = None
