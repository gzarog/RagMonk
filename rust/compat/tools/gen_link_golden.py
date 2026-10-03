"""Generate RUST-08 knowledge-link golden fixtures from the Python reference.

    python rust/compat/tools/gen_link_golden.py

Indexes rust/compat/fixtures/linking with the reference (local mode, in a
temporary RAGMONK_HOME) and records every cross-domain link in a canonical,
id-free form:

    entity    = (relative file path, qualified name, kind, start line)
    document  = relative file path
    section   = chunk ordinal within the document
    link      = type, resolver, confidence, evidence

Writes rust/compat/golden/links.json.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "fixtures" / "linking"
OUT = ROOT / "golden" / "links.json"


def ragmonk(home: str, *args: str) -> None:
    env = {**os.environ, "RAGMONK_HOME": home}
    exe = Path(sys.executable).with_name("ragmonk")
    subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)


def main() -> None:
    with tempfile.TemporaryDirectory() as home:
        ragmonk(home, "init")
        ragmonk(home, "source", "add", str(CORPUS))
        ragmonk(home, "index")
        (db,) = Path(home, "projects").glob("*/knowledge.db")
        conn = sqlite3.connect(db)
        root = str(CORPUS.resolve())

        def rel(path: str) -> str:
            return os.path.relpath(path, root).replace(os.sep, "/")

        rows = conn.execute(
            """
            SELECT l.link_type, l.resolver, l.confidence, l.evidence,
                   ef.path, e.qualified_name, e.kind, e.start_line,
                   df.path, s.order_index
            FROM cross_links l
            JOIN entities e ON e.id = l.entity_id
            JOIN files ef ON ef.id = e.file_id
            JOIN documents d ON d.id = l.document_id
            JOIN files df ON df.id = d.file_id
            LEFT JOIN document_sections s ON s.id = l.section_id
            """
        ).fetchall()
    links = [
        {
            "entity": [rel(r[4]), r[5], r[6], r[7]],
            "document": rel(r[8]),
            "section": r[9],
            "type": r[0],
            "resolver": r[1],
            "confidence": r[2],
            "evidence": r[3],
        }
        for r in rows
    ]
    links.sort(key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    OUT.write_text(
        json.dumps({"links": links}, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    )
    print(f"wrote {OUT} ({len(links)} links)")


if __name__ == "__main__":
    main()
