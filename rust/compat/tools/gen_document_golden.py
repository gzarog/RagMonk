"""Generate RUST-06 document-core golden fixtures from the Python reference.

    python rust/compat/tools/gen_document_golden.py

Records, from the Python reference:

* the canonical normalized-document JSON (the intermediate format the Rust
  chunker consumes) for every file in rust/compat/fixtures/documents,
  converted through the reference's Docling adapter and normalizer, plus
  hand-built normalized documents covering table/heading/Unicode edge
  cases;
* the chunks the reference chunker produces for each of them under
  several chunking configurations (with diagnostics);
* exact token counts and token-budget splits for a Unicode corpus;
* tokenizer identity and the document version stamp.

Writes rust/compat/golden/documents.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ragmonk.core.config import ChunkingConfig
from ragmonk.documents import chunker, docling_adapter, normalizer, pipeline, tokenization
from ragmonk.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragmonk.tokenization import model_identity
from ragmonk.tokenization.model_tokenizer import get_model_tokenizer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "documents"
OUT = ROOT / "golden" / "documents.json"

CONFIGS = {
    "default": {},
    "small": {"max_tokens": 64, "min_tokens": 10, "overlap_tokens": 8, "safety_tokens": 2},
    "tiny": {
        "max_tokens": 32,
        "min_tokens": 4,
        "overlap_tokens": 0,
        "safety_tokens": 0,
        "merge_peers": False,
    },
}

TOKEN_CORPUS = [
    "",
    "hello",
    "Hello, world!",
    "The quick brown fox jumps over the lazy dog.",
    "Η γρήγορη καφέ αλεπού πηδάει πάνω από τον τεμπέλη σκύλο.",
    "日本語のテキストと中文字符",
    "Emoji 🚀🔥👍🏽 and flags 🇬🇷",
    "café naïve Zoë résumé ﬁ ligature",
    "é combining and é precomposed",
    "zero​width‍joiner﻿bom",
    "RTL: שלום עולם مرحبا",
    "tabs\tand\nnewlines\r\nmixed   spaces",
    "\u0000\u0007control\u001fchars",
    "https://example.com/a/very/long/path?query=supercalifragilisticexpialidocious&x=1",
    'fn main() { println!("{}", x.iter().map(|v| v * 2).sum::<i64>()); }',
    "supercalifragilisticexpialidocious" * 4,
    "a" * 300,
    "Ⅻ ① ² ⁴ ½ ∞ ≠ ≤",
    "  leading and trailing spaces  ",
    "UPPER lower MiXeD",
]

SENTENCE_CORPUS = [
    "One. Two! Three? Four",
    "No boundary here",
    "  spaced.   Out!  ",
    "Ellipsis... then more. End.",
    "Mr. Smith went to Washington. He left.",
    "Ελληνικά. Και άλλα;",
    "",
    "   ",
]


def unit(kind: str, text: str = "", **kw: object) -> NormalizedUnit:
    defaults: dict[str, object] = {
        "heading_level": None,
        "heading_path": (),
        "parent_index": None,
        "page_start": None,
        "page_end": None,
    }
    defaults.update(kw)
    return NormalizedUnit(kind=kind, text=text, **defaults)  # type: ignore[arg-type]


def synthetic() -> dict[str, NormalizedDocument]:
    long_heading = " ".join(f"word{i}" for i in range(120))
    long_cell = " ".join(f"cellword{i}" for i in range(400))
    rows = tuple((f"row{i}", f"value {i} " * (i % 5 + 1), "x" * (i % 7)) for i in range(25))
    return {
        "synthetic/oversized-table": NormalizedDocument(
            title="Synthetic tables",
            page_count=3,
            is_scanned=False,
            units=[
                unit("heading", "Synthetic tables", heading_level=0),
                unit(
                    "table",
                    heading_path=("Synthetic tables",),
                    parent_index=0,
                    page_start=1,
                    page_end=2,
                    table_rows=(("Name", "Value", "Pad"), *rows),
                    caption="Big table caption",
                    header_row_count=1,
                ),
                unit(
                    "table",
                    heading_path=("Synthetic tables",),
                    parent_index=0,
                    page_start=3,
                    page_end=3,
                    table_rows=(("A", "B", "C"), ("short", long_cell, "tail")),
                    header_row_count=1,
                ),
                unit(
                    "table",
                    heading_path=("Synthetic tables",),
                    parent_index=0,
                    table_rows=(("H1", "H2"), ("sub1", "sub2"), ("d1", "d2")),
                    header_row_count=2,
                ),
                unit("table", heading_path=("Synthetic tables",), parent_index=0, table_rows=()),
            ],
        ),
        "synthetic/long-headings": NormalizedDocument(
            title="A document title that is itself rather long " * 6,
            page_count=None,
            is_scanned=False,
            units=[
                unit("heading", "Root", heading_level=1),
                unit(
                    "heading", long_heading, heading_level=2, heading_path=("Root",), parent_index=0
                ),
                unit(
                    "heading",
                    "Leaf",
                    heading_level=3,
                    heading_path=("Root", long_heading),
                    parent_index=1,
                ),
                unit(
                    "paragraph",
                    "Body under a very long breadcrumb. " * 30,
                    heading_path=("Root", long_heading, "Leaf"),
                    parent_index=2,
                    page_start=4,
                    page_end=5,
                ),
                unit(
                    "paragraph",
                    "x" * 900,
                    heading_path=("Root", long_heading, "Leaf"),
                    parent_index=2,
                ),
            ],
        ),
        "synthetic/unicode": NormalizedDocument(
            title=None,
            page_count=1,
            is_scanned=True,
            units=[
                unit("paragraph", t, page_start=1, page_end=1) for t in TOKEN_CORPUS if t.strip()
            ],
        ),
        "synthetic/empty": NormalizedDocument(
            title=None, page_count=None, is_scanned=False, units=[]
        ),
    }


def unit_json(u: NormalizedUnit) -> dict:
    d = asdict(u)
    d["heading_path"] = list(u.heading_path)
    d["table_rows"] = None if u.table_rows is None else [list(r) for r in u.table_rows]
    return d


def doc_json(d: NormalizedDocument) -> dict:
    return {
        "title": d.title,
        "page_count": d.page_count,
        "is_scanned": d.is_scanned,
        "units": [unit_json(u) for u in d.units],
    }


def chunk_json(c: chunker.Chunk) -> dict:
    d = asdict(c)
    d["heading_path"] = list(c.heading_path)
    d["table_rows"] = None if c.table_rows is None else [list(r) for r in c.table_rows]
    return d


def main() -> None:
    docs: dict[str, NormalizedDocument] = {}
    for path in sorted(p for p in FIXTURES.iterdir() if p.is_file()):
        fmt = docling_adapter.detect_format(path)
        conversion = docling_adapter.convert(path)
        docs[path.name] = normalizer.normalize(conversion.document, fmt)
    docs.update(synthetic())

    tok = get_model_tokenizer()
    documents = {}
    for name, doc in docs.items():
        title = doc.title or ""
        chunked = {}
        for cfg_name, cfg in CONFIGS.items():
            diag = chunker.ChunkingDiagnostics()
            chunks = chunker.chunk_document(
                doc, config=ChunkingConfig(**cfg), doc_title=title, diagnostics=diag
            )
            chunked[cfg_name] = {
                "chunks": [chunk_json(c) for c in chunks],
                "diagnostics": asdict(diag),
            }
        documents[name] = {"normalized": doc_json(doc), "doc_title": title, "chunks": chunked}

    tokens = []
    for text in TOKEN_CORPUS:
        entry = {
            "text": text,
            "with_special": tok.count(text, add_special_tokens=True),
            "body": tok.count(text, add_special_tokens=False),
            "split": {str(b): tok.split(text, b) for b in (1, 3, 7) if text},
            "budget_split": {
                str(b): tokenization.split_by_token_budget(text, b) for b in (2, 5, 16) if text
            },
        }
        tokens.append(entry)
    sentences = [{"text": t, "sentences": tokenization.split_sentences(t)} for t in SENTENCE_CORPUS]
    stamp = pipeline.document_version_stamp()
    out = {
        "configs": CONFIGS,
        "identity": {
            "model_id": model_identity.EMBEDDING_MODEL_ID,
            "revision": model_identity.TOKENIZER_REVISION,
            "fingerprint": model_identity.tokenizer_fingerprint(),
            "preprocessing": model_identity.preprocessing_fingerprint(),
            "max_sequence_tokens": model_identity.MAX_SEQUENCE_TOKENS,
            "manifest": model_identity.TOKENIZER_ASSET_MANIFEST,
            "parser_version": stamp.parser_version,
            "chunker_version": stamp.chunker_version,
            "embedding_text_version": stamp.embedding_text_version,
        },
        "tokens": tokens,
        "sentences": sentences,
        "documents": documents,
    }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
