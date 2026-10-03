"""Generate RUST-09 semantic-search golden fixtures from the Python reference.

    python rust/compat/tools/gen_semantic_golden.py

Indexes rust/compat/fixtures/linking with semantic search enabled (local
mode, temporary RAGMONK_HOME), embeds each query in
rust/compat/fixtures/embeddings/queries.json with the reference embedder,
and ranks every stored vector exactly by cosine similarity. Each hit is
recorded in a canonical, id-free form:

    entity   = ("entity", relative path, qualified name, start line)
    section  = ("section", relative path, ordinal within the document)

Writes rust/compat/golden/semantic.json (top 10 per query, scores rounded).
"""

from __future__ import annotations

import array
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from ragmonk.retrieval.embedder import embed_texts

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "fixtures" / "linking"
QUERIES = ROOT / "fixtures" / "embeddings" / "queries.json"
OUT = ROOT / "golden" / "semantic.json"
TOP = 10


def ragmonk(home: str, *args: str) -> None:
    env = {**os.environ, "RAGMONK_HOME": home}
    exe = Path(sys.executable).with_name("ragmonk")
    subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)


def main() -> None:
    queries = json.loads(QUERIES.read_text(encoding="utf-8"))["queries"]
    with tempfile.TemporaryDirectory() as home:
        ragmonk(home, "init")
        ragmonk(home, "config", "set", "search.semantic", "true")
        ragmonk(home, "source", "add", str(CORPUS))
        ragmonk(home, "index")
        (db,) = Path(home, "projects").glob("*/knowledge.db")
        conn = sqlite3.connect(db)
        root = str(CORPUS.resolve())

        def rel(path: str) -> str:
            return os.path.relpath(path, root).replace(os.sep, "/")

        subjects = []
        for kind, path, name, line, blob in conn.execute(
            """
            SELECT 'entity', f.path, e.qualified_name, e.start_line, v.vector
            FROM embeddings v JOIN entities e ON e.id = v.subject_id
            JOIN files f ON f.id = e.file_id WHERE v.subject_type = 'entity'
            UNION ALL
            SELECT 'section', f.path, NULL, s.order_index, v.vector
            FROM embeddings v JOIN document_sections s ON s.id = v.subject_id
            JOIN documents d ON d.id = s.document_id JOIN files f ON f.id = d.file_id
            WHERE v.subject_type = 'document_section'
            """
        ):
            vec = array.array("f")
            vec.frombytes(blob)
            key = [kind, rel(path), name, line] if kind == "entity" else [kind, rel(path), line]
            subjects.append((key, vec.tolist()))
        vectors = embed_texts(queries)
        out = []
        for query, qv in zip(queries, vectors, strict=True):
            scored = sorted(
                ((sum(a * b for a, b in zip(qv, v, strict=True)), key) for key, v in subjects),
                key=lambda t: (-t[0], json.dumps(t[1])),
            )[:TOP]
            out.append(
                {
                    "query": query,
                    "hits": [{"key": key, "score": round(score, 5)} for score, key in scored],
                }
            )
        payload = {"subjects": len(subjects), "results": out}
        OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {OUT} ({len(out)} queries over {len(subjects)} subjects)")


if __name__ == "__main__":
    main()
