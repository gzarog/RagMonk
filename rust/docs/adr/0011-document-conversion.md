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

## Slice 2: PDF and OCR

### PDF text layer

- PDFs are converted through docling.rs's `pdf-text` feature: pure Rust
  (`lopdf` plus a text parser), with no ML models and no network access.
- `documents.pdf_mode` `accurate` and `fast` share this path in this build.
  The ML layout/table pipeline stays an opt-in, per the user's decision.
- A PDF with no text layer becomes an empty document that keeps its pages, so
  it is flagged `is_scanned`.

### OCR policy

This is the reference's `_convert_pdf` policy:

- `always` tries OCR first.
- `auto` OCRs only when the plain result looks scanned. It uses the same
  low-text-density check as `is_scanned`.
- `off` never OCRs.
- If OCR is unavailable or fails, the plain result is kept.

### OCR engine

- **Engine:** `ocrs` `=0.12.0` on `rten` 0.24. Newer `ocrs`/`rten` releases
  need Rust 1.94, while the workspace pins 1.90, so we stay on these.
- **Input:** page images are extracted from the PDF with `lopdf`. JPEG and
  JPEG 2000 images are decoded with `image`. Flate-compressed 8-bit
  DeviceRGB and DeviceGray images are decoded as raw pixels.
- **Models:** they are not bundled. They load from `RAGMONK_OCR_MODELS_DIR`,
  else `<home>/models/ocrs`. Both files are verified against pinned SHA-256
  digests (`OCR_MODEL_MANIFEST`). Missing or modified models make OCR
  unavailable; that is logged once and the plain result is kept.
- **CI:** the workflow downloads the pinned models (cached and
  SHA-verified) and sets `RAGMONK_REQUIRE_OCR=1`, so the OCR tests can never
  silently skip.
- **Images:** `.png`/`.jpg`/`.tif` files are OCR'd only when
  `documents.image_ocr` is on. Otherwise they are indexed with no derived
  content, as in the reference.

### Page limit

- PDFs with more pages than `documents.max_pages` are recorded as
  `skipped_limit`. Processors report this with the `SKIPPED_LIMIT` error
  code, and the coordinator records it as a limit, not a failure.

### Conversion cache

- A file cache under `<home>/v2/cache/conversion` covers PDF and OCR
  results.
- The key is the content hash plus the OCR/settings key, the converter
  version and the OCR engine version.
- Writes are atomic (temp file + rename). Corrupt entries are ignored and
  rebuilt.
- Workers use it without touching SQLite.

### Debug-build performance

`rten`, `ocrs`, image decoding and `tokenizers` are built with `opt-level=3`
in the dev profile. That brought the OCR test suite from 72 s to 7 s.

### Benchmark

100 PDFs, half of them image-only scans needing OCR:

| | Time |
|---|---|
| Rust | 14.6 s cold |
| Python reference (Docling accurate pipeline + OCR) | 69.7 s |

Both recover the scanned text.
