# ADR 0011: Rust-native document conversion (RUST-07, slice 1)

Status: accepted

## Decisions

The user chose these options before implementation:

1. **PDF.** Text-layer conversion by default; the docling.rs ML pipeline is
   opt-in only.
2. **OCR.** Pure-Rust `ocrs`.
3. **Delivery.** RUST-07 is split into slices on
   `feature/rust-07-docling-documents`:
   1. converter, normalizer, processor, and the non-PDF/non-email formats
      (this slice);
   2. PDF text layer and OCR;
   3. EML parsing and attachments.

## Converter

- `ragmonk-convert` integrates `docling` (docling.rs), pinned at `=1.89.0`,
  with `default-features = false`. That means no ONNX Runtime, no model
  weights, no ASR/VLM and no network access.
- It sits behind a `DocumentConverter` trait. The trait returns Docling
  document JSON (`export_to_json_value`), which is the same schema Python
  Docling exports.
- Formats in this slice: Markdown, TXT (routed through the Markdown backend,
  like the reference), HTML, CSV, DOCX, PPTX, XLSX, ODT/ODS/ODP and EPUB.
- PDF, EML and images are recognized but report `Unsupported`. They are
  indexed with no derived content until their slices land, which is the
  reference's "detected, not converted" behaviour.
- A backend panic is contained per file. Corrupt input fails only that
  file, permanently, with `conversion_error`.

## Normalizer

`normalize.rs` ports `documents/normalizer.py` onto Docling JSON. It
reproduces `iterate_items()` exactly:

- only the body layer is included;
- groups are flattened;
- picture children are limited to captions;
- table captions are consumed;
- header-row detection, the heading stack and title selection follow the
  reference;
- the low-text-density scanned check is included.

## Parity

- **Normalizer:** on the reference's own Docling JSON
  (`compat/golden/docling.json`, 14 documents), the Rust normalizer equals
  the Python normalizer exactly.
- **Converter:** docling.rs plus the Rust normalizer reproduces the
  reference's normalized documents byte-for-byte for all 14 fixtures. The
  test requires exact equality, so any drift in the pinned converter is
  caught.
- **End to end:** documents indexed through the V2 coordinator store chunks
  equal to the reference pipeline's chunks. That includes text, search text,
  contextual text, token counts, heading paths and table rows.

## Workspace note

`docling-core` enables serde_json's `preserve_order` workspace-wide.
Production JSON is unaffected: the compat gate is identical. Two golden
tests that sorted by serialized JSON now use an order-independent key.

## Compat manifest

The `docs --json` step needs `init`, `source add` and `index` (RUST-12)
before it can run. It is retagged to RUST-12, the same treatment RUST-04
gave `reindex-unchanged`.

## Benchmark

600 mixed documents (md/html/txt/docx/pptx/xlsx):

| Pass | Rust | Python CLI |
|---|---|---|
| Cold | 7.5 s | 37.3 s |
| Warm | 0.02 s | 1.8 s |
| Single edit | 0.03 s | 9.0 s |
