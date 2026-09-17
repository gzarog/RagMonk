"""Exact Tokenizer plan, Phase 2: the chunker budgets the full contextual
payload against the exact embedding tokenizer.

These tests assert the plan's hard invariant -- every embeddable chunk's
contextual payload (with the model's special tokens) fits
``resolved_max_tokens - safety_tokens`` -- across English, Greek and code,
plus the deterministic long-header reduction policy and the diagnostics
counters. They run in the default (offline) suite.
"""

from __future__ import annotations

from ragmonk.core.config import ChunkingConfig
from ragmonk.documents.chunker import ChunkingDiagnostics, chunk_document
from ragmonk.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragmonk.tokenization.model_tokenizer import get_model_tokenizer


def _heading(text: str, *, level: int = 0, heading_path: tuple[str, ...] = (), parent=None):  # noqa: ANN001, ANN202
    return NormalizedUnit(
        kind="heading",
        text=text,
        heading_level=level,
        heading_path=heading_path,
        parent_index=parent,
        page_start=None,
        page_end=None,
    )


def _paragraph(text: str, *, heading_path: tuple[str, ...] = (), parent=None):  # noqa: ANN001, ANN202
    return NormalizedUnit(
        kind="paragraph",
        text=text,
        heading_level=None,
        heading_path=heading_path,
        parent_index=parent,
        page_start=None,
        page_end=None,
    )


def _doc(units: list[NormalizedUnit]) -> NormalizedDocument:
    return NormalizedDocument(title=None, page_count=None, is_scanned=False, units=units)


def _payload_tokens(text: str) -> int:
    return get_model_tokenizer().count(text, add_special_tokens=True)


_GREEK = (
    "Η ανάκτηση πληροφορίας βασίζεται στη σημασιολογική ομοιότητα μεταξύ "
    "του ερωτήματος και των εγγράφων. "
) * 8
_CODE = (
    "public async Task<IReadOnlyList<int>> GetCountsAsync(CancellationToken ct) "
    "=> await _repository.Query(x => x.IsActive).Select(x => x.Id).ToListAsync(ct); "
) * 6
_ENGLISH = (
    "The retrieval engine budgets every chunk against the exact tokenizer of "
    "the embedding model so no payload is silently truncated at inference time. "
) * 8


def test_every_chunk_payload_fits_the_model_budget_mixed_corpus() -> None:
    cfg = ChunkingConfig()  # auto -> 256, safety 4
    bound = cfg.resolved_max_tokens - cfg.safety_tokens
    units = [
        _heading("Retrieval", heading_path=()),
        _paragraph(_ENGLISH, heading_path=("Retrieval",), parent=0),
        _heading("Ελληνικά", heading_path=()),
        _paragraph(_GREEK, heading_path=("Ελληνικά",), parent=2),
        _heading("Code", heading_path=()),
        _paragraph(_CODE, heading_path=("Code",), parent=4),
    ]
    diagnostics = ChunkingDiagnostics()
    chunks = chunk_document(
        _doc(units), config=cfg, doc_title="Exact Tokenizer Manual", diagnostics=diagnostics
    )

    assert chunks
    for chunk in chunks:
        assert _payload_tokens(chunk.contextual_text) <= bound, chunk.kind
    assert diagnostics.max_payload_tokens <= bound
    # The long paragraphs must actually have been split by the budget.
    assert diagnostics.chunks_split_by_budget >= 1


def test_payload_fits_for_small_explicit_ceiling() -> None:
    cfg = ChunkingConfig(max_tokens=64, min_tokens=8, overlap_tokens=8, safety_tokens=2)
    bound = cfg.resolved_max_tokens - cfg.safety_tokens
    units = [
        _heading("Section", heading_path=()),
        _paragraph(_ENGLISH, heading_path=("Section",), parent=0),
        _paragraph(_GREEK, heading_path=("Section",), parent=0),
    ]
    chunks = chunk_document(_doc(units), config=cfg, doc_title="Doc")
    assert len(chunks) > 2
    for chunk in chunks:
        assert _payload_tokens(chunk.contextual_text) <= bound


def test_long_heading_path_is_reduced_deepest_kept_body_intact() -> None:
    # A deep breadcrumb of long headings would consume the whole budget.
    ancestors = (
        "The Very First Ancestor Heading With Many Descriptive Words Indeed",
        "A Second Intermediate Ancestor Heading Also Quite Long And Wordy",
        "Third Intermediate Ancestor Heading Carrying Yet More Context Words",
    )
    deepest = "Current Deepest Section"
    heading_path = (*ancestors, deepest)
    body = "the cat dog runs fast today"
    # header_budget = (48 - 0 - 2) - min(8, 45) = 38, below the full
    # 41-token breadcrumb, so the reduction policy must engage.
    cfg = ChunkingConfig(max_tokens=48, min_tokens=8, overlap_tokens=0, safety_tokens=0)
    units = [_paragraph(body, heading_path=heading_path, parent=None)]

    diagnostics = ChunkingDiagnostics()
    chunks = chunk_document(_doc(units), config=cfg, doc_title="Title", diagnostics=diagnostics)

    assert len(chunks) == 1
    chunk = chunks[0]
    bound = cfg.resolved_max_tokens - cfg.safety_tokens
    assert _payload_tokens(chunk.contextual_text) <= bound
    # Reduction happened and was counted.
    assert diagnostics.context_headers_reduced >= 1
    # The deepest/current heading is always preserved...
    assert deepest in chunk.contextual_text
    # ...the oldest ancestor is dropped first...
    assert ancestors[0] not in chunk.contextual_text
    # ...and the evidence body is never truncated to keep a breadcrumb.
    assert body in chunk.contextual_text


def test_no_reduction_when_header_fits() -> None:
    cfg = ChunkingConfig()  # generous 256 budget
    units = [_paragraph("A short body.", heading_path=("Intro",), parent=None)]
    diagnostics = ChunkingDiagnostics()
    chunk_document(_doc(units), config=cfg, doc_title="Guide", diagnostics=diagnostics)
    assert diagnostics.context_headers_reduced == 0


def test_diagnostics_optional_and_default_none() -> None:
    # Existing callers pass no diagnostics; chunking still works.
    units = [_paragraph("Body text here.", heading_path=("H",), parent=None)]
    chunks = chunk_document(_doc(units), doc_title="T")
    assert len(chunks) == 1
    assert chunks[0].contextual_text.endswith("Body text here.")
