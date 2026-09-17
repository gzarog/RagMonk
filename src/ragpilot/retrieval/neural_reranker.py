"""Optional final neural reranking pass (Search Quality Improvement Plan,
Phase 11 -- explicitly OPTIONAL in the plan, and NOT enabled by default).

Reorders only the top ``search.reranker.top_n`` hits of ``retrieval/
reranker.py``'s already RRF-fused hybrid tier, using a real local
cross-encoder's ``(query, passage)`` relevance score -- never applied to
the whole candidate pool, and never a replacement for the deterministic
RRF pipeline (``retrieval/fusion.py``/``retrieval/merger.py``/``retrieval/
reranker.py``), which stays exactly as Phase 8 left it either way. Called
by ``cli/search.py`` only when ``search.reranker.enabled`` is true and only
within the already-additive ``--hybrid`` view, so the default (disabled)
search path never imports this module's model-loading code at all.

Uses ``cross-encoder/ms-marco-MiniLM-L-6-v2`` through plain
``transformers`` calls (``AutoModelForSequenceClassification``, one
relevance logit per pair), the same "direct ``transformers`` call, no
``sentence-transformers`` dependency" choice ``retrieval/embedder.py``
makes and for the same reason: ``torch``/``transformers`` are already
required, non-optional dependencies (Docling's PDF pipeline), so loading
one more fixed, well-known model through them adds no new package, only a
weights download. ``cross-encoder/ms-marco-MiniLM-L-6-v2`` is the standard,
permissively-licensed (Apache 2.0), 6-layer MiniLM cross-encoder trained
for exactly this task -- MS MARCO passage relevance ranking -- and small
enough (~90 MB of weights) to load and run a top-20 batch in one forward
pass without materially changing this project's local-first footprint.

Unlike ``embedder.py``, a cross-encoder scores a *pair* jointly (query and
passage attend to each other in the same forward pass) rather than
producing two independent vectors to compare by cosine similarity -- that
joint attention is exactly what makes it a stronger, but far more
expensive per-comparison, relevance signal than RRF's rank-only fusion or
semantic search's embedding similarity. Runs offline once weights are
cached (``huggingface_hub``'s default cache, same as ``embedder.py``); see
the ``reranker_model`` pytest marker (``tests/unit/test_neural_reranker.py``,
CONTRIBUTING.md) for how the default test suite avoids depending on a real
download. Kept as its own marker rather than reusing ``embedding_model``:
the two are independent optional models, gated by independent config
flags (``search.semantic`` vs. ``search.reranker.enabled``), and a
contributor/CI job may reasonably want to exercise one without the other.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from ragpilot.telemetry.logging import get_logger, log_event

if TYPE_CHECKING:
    from ragpilot.retrieval.reranker import RankedHit

RERANKER_MODEL_ID = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_MAX_TOKENS = 256
_BATCH_SIZE = 16
_MAX_CHARS = 4000  # same defensive pre-tokenizer cap as embedder.py

_logger = get_logger("neural_reranker")


class NeuralRerankerUnavailableError(Exception):
    """The cross-encoder model could not be loaded: ``torch``/
    ``transformers`` missing, no cached weights and no network, a corrupt
    cache, or any other Hugging Face Hub/local-load failure. A specific,
    catchable type (never a bare ``ImportError`` or library-specific
    exception), mirroring ``embedder.EmbeddingModelUnavailableError`` --
    ``rerank_hits`` catches exactly this and falls back to the existing
    RRF-only ranking instead of letting ``search --hybrid`` crash.
    """


_lock = threading.Lock()
_handle: tuple[Any, Any] | None = None  # (tokenizer, model), loaded at most once per process


def _load_model() -> tuple[Any, Any]:
    global _handle
    with _lock:
        if _handle is not None:
            return _handle
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise NeuralRerankerUnavailableError(
                f"transformers/torch is not installed: {exc}"
            ) from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(RERANKER_MODEL_ID)
            model = AutoModelForSequenceClassification.from_pretrained(RERANKER_MODEL_ID)
        except Exception as exc:  # noqa: BLE001 - any HF Hub/cache failure -> unavailable
            raise NeuralRerankerUnavailableError(
                f"failed to load reranker model {RERANKER_MODEL_ID!r}: {exc}"
            ) from exc
        model.eval()
        _handle = (tokenizer, model)
        return _handle


def score_pairs(query: str, texts: Sequence[str]) -> list[float]:
    """One relevance score per ``texts[i]`` against ``query``, in the same
    order, batched ``_BATCH_SIZE`` pairs per forward pass rather than one
    model call per candidate -- the whole point of reranking only a
    bounded top-N pool instead of the full candidate set.

    Raises ``NeuralRerankerUnavailableError`` if the model cannot be
    loaded at all; never partially scores some texts and not others.
    """
    if not texts:
        return []
    tokenizer, model = _load_model()
    import torch

    scores: list[float] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        batch = [text[:_MAX_CHARS] for text in texts[start : start + _BATCH_SIZE]]
        encoded = tokenizer(
            [query] * len(batch),
            batch,
            padding=True,
            truncation=True,
            max_length=_MAX_TOKENS,
            return_tensors="pt",
        )
        with torch.no_grad():
            output = model(**encoded)
        logits = output.logits.squeeze(-1)
        scores.extend(logits.tolist() if logits.dim() > 0 else [logits.item()])
    return scores


def _hit_text(hit: RankedHit) -> str:
    """What gets scored against the query: a candidate's matched snippet
    when there is one (the actual evidence a cross-encoder should judge),
    falling back to its title -- never empty, since an empty passage
    carries no signal for the model to score.
    """
    candidate = hit.candidate
    return candidate.snippet or candidate.title


def rerank_hits(
    query: str,
    hits: Sequence[RankedHit],
    *,
    top_n: int,
    score_batch: Callable[[str, Sequence[str]], list[float]] | None = None,
) -> list[RankedHit]:
    """Reorders only ``hits[:top_n]`` by ``score_batch(query, texts)``,
    descending, appending the untouched remainder (``hits[top_n:]``, still
    in its original RRF order) after -- see the module docstring for why
    only a bounded prefix is ever rescored.

    Falls back to ``list(hits)`` unchanged, logging a warning, whenever
    ``score_batch`` raises ``NeuralRerankerUnavailableError`` (no model
    available) or when there is nothing worth rescoring (``top_n <= 0`` or
    fewer than two candidates) -- this must never turn an optional
    reranking pass into a hard search failure.

    ``score_batch`` defaults to ``None``, resolved to the module-level
    ``score_pairs`` (which lazily loads the cross-encoder on first use)
    *inside* this call rather than as a bound default argument -- so a
    test that monkeypatches ``neural_reranker.score_pairs`` also affects
    every caller that omits ``score_batch`` (``cli/search.py`` included),
    not just callers that happened to be defined after the patch. Unit
    tests instead pass a stub directly, to exercise batching/ordering/
    fallback without a real model -- see ``tests/unit/test_neural_reranker.py``.
    """
    hit_list = list(hits)
    if top_n <= 0 or len(hit_list) < 2:
        return hit_list

    candidates = hit_list[:top_n]
    remainder = hit_list[top_n:]
    texts = [_hit_text(h) for h in candidates]
    resolved_score_batch = score_batch if score_batch is not None else score_pairs

    try:
        scores = resolved_score_batch(query, texts)
    except NeuralRerankerUnavailableError as exc:
        # INFO, not WARNING: this call can happen mid-``ragpilot search
        # --json``, whose stdout is machine-parsed, and the console
        # handler echoes WARNING+ to that same stream (see
        # ``telemetry/logging.py``) -- the same reason ``retrieval/
        # semantic.py``'s own graceful-degradation path never logs at
        # WARNING either, only returns a structured reason. Still fully
        # visible in the JSON log file for diagnostics.
        log_event(
            _logger,
            "neural_reranker_unavailable",
            reason=str(exc),
            model_id=RERANKER_MODEL_ID,
        )
        return hit_list

    if len(scores) != len(candidates):
        log_event(
            _logger,
            "neural_reranker_score_count_mismatch",
            expected=len(candidates),
            got=len(scores),
        )
        return hit_list

    reordered = [
        hit
        for _score, hit in sorted(
            zip(scores, candidates, strict=True), key=lambda pair: -pair[0]
        )
    ]
    return [*reordered, *remainder]
