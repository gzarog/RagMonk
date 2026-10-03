"""Generate RUST-07 email golden fixtures from the Python reference.

    python rust/compat/tools/gen_email_golden.py

For every .eml in rust/compat/fixtures/documents: the parent email's
normalized document (Docling EMAIL backend + normalizer), the attachment
enumeration (ordinals, names, types, sizes, content ids, skip reasons) under
default and tight limits, and each converted attachment's title/format/
chunk texts. Writes rust/compat/golden/email.json.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ragmonk.core.config import ChunkingConfig
from ragmonk.documents import attachment_pipeline, docling_adapter, normalizer
from ragmonk.documents.attachment_pipeline import AttachmentSkipped
from ragmonk.documents.email_attachments import EmailAttachmentSettings, extract_attachments

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "documents"
OUT = ROOT / "golden" / "email.json"

LIMITS = {
    "default": EmailAttachmentSettings(),
    "tight": EmailAttachmentSettings(max_bytes=2000, max_count=2, total_max_bytes=3000),
}


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
    for path in sorted(FIXTURES.glob("*.eml")):
        doc = docling_adapter.convert(path).document
        entry: dict = {
            "normalized": doc_json(normalizer.normalize(doc, docling_adapter.detect_format(path)))
        }
        for name, settings in LIMITS.items():
            ex = extract_attachments(path, settings)
            atts = []
            for part in ex.attachments:
                item = {
                    "ordinal": part.ordinal,
                    "filename": part.filename,
                    "content_type": part.content_type,
                    "content_id": part.content_id,
                    "size": part.decoded_size,
                }
                try:
                    prepared = attachment_pipeline.prepare_attachment(
                        part,
                        cache_conn=None,
                        ocr_mode="off",
                        image_ocr=False,
                        chunking=ChunkingConfig(),
                        max_document_pages=1000,
                    )
                    item["result"] = {
                        "format": prepared.doc_format.value,
                        "title": prepared.meta.title,
                        "chunks": [c.text for c in prepared.chunks],
                    }
                except AttachmentSkipped as skip:
                    item["result"] = {"skipped": skip.reason}
                except Exception as exc:  # noqa: BLE001 - recorded as an attachment failure
                    item["result"] = {"failed": type(exc).__name__}
                atts.append(item)
            entry[name] = {
                "attachments": atts,
                "skipped": [
                    {
                        "ordinal": s.ordinal,
                        "filename": s.filename,
                        "content_type": s.content_type,
                        "size": s.size,
                        "reason": s.reason,
                    }
                    for s in ex.skipped
                ],
            }
        out[path.name] = entry
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
