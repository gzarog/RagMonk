"""Generate RUST-07 normalizer/conversion golden fixtures from the Python reference.

    python rust/compat/tools/gen_docling_golden.py

For every convertible (non-PDF, non-email) file in rust/compat/fixtures/documents,
records the reference's Docling document as JSON (`export_to_dict`) and the
normalized document the reference's normalizer produces from it. The Rust
normalizer must reproduce `normalized` exactly from `docling`; the Rust converter
(docling.rs) is compared against `normalized` as a quality reference.
Writes rust/compat/golden/docling.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ragmonk.documents import docling_adapter, normalizer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "documents"
OUT = ROOT / "golden" / "docling.json"
SKIP_SUFFIXES = {".pdf", ".eml", ".png"}


def doc_json(d: normalizer.NormalizedDocument) -> dict:
    units = []
    for u in d.units:
        x = asdict(u)
        x["heading_path"] = list(u.heading_path)
        x["table_rows"] = None if u.table_rows is None else [list(r) for r in u.table_rows]
        units.append(x)
    return {
        "title": d.title,
        "page_count": d.page_count,
        "is_scanned": d.is_scanned,
        "units": units,
    }


def main() -> None:
    out = {}
    for path in sorted(p for p in FIXTURES.iterdir() if p.is_file()):
        if path.suffix.lower() in SKIP_SUFFIXES or path.name.startswith("corrupt"):
            continue
        fmt = docling_adapter.detect_format(path)
        doc = docling_adapter.convert(path).document
        out[path.name] = {
            "format": fmt.value,
            "docling": doc.export_to_dict(),
            "normalized": doc_json(normalizer.normalize(doc, fmt)),
        }
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
