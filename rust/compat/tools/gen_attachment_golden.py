"""Generate RUST-10 attachment-provenance golden fixtures from the reference.

    python rust/compat/tools/gen_attachment_golden.py

Indexes rust/compat/fixtures/attachments (an email with text, Markdown,
Office and archive attachments) with semantic search on, then records
where search hits inside attachments point:

* ``lexical``: each hit's kind, tier and ``location.attachment``;
* ``semantic``: the top hits' ``location.attachment`` (exact vector
  search, see gen_search_golden).

Parent document ids are dropped. Writes rust/compat/golden/attachments.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_search_golden  # noqa: E402,F401  (pins exact vector search)

from ragmonk.core.lifecycle import AppContext  # noqa: E402
from ragmonk.retrieval import lexical, semantic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "golden" / "attachments.json"
QUERIES = ["zebrafish", "narwhalmemo", "Quarterly planning", "migration notes", "memo decision"]


def attachment(location: dict | None) -> dict | None:
    att = (location or {}).get("attachment")
    if att is None:
        return None
    return {k: v for k, v in att.items() if k != "parent_document_id"}


def main() -> None:
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as home:
        corpus = Path(home) / "corpus"
        shutil.copytree(ROOT / "fixtures" / "attachments", corpus)
        env = {**os.environ, "RAGMONK_HOME": home, "RAGMONK_SEARCH__SEMANTIC": "true"}
        for args in (["init"], ["source", "add", str(corpus)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)
        os.environ.update(
            RAGMONK_HOME=home,
            RAGMONK_SEARCH__SEMANTIC="true",
            RAGMONK_SEARCH__CACHE__ENABLED="false",
        )
        ctx = AppContext.bootstrap(home=Path(home), cwd=corpus.parent)
        out = []
        try:
            for q in QUERIES:
                lex = lexical.search(ctx, q, limit=20)
                sem = semantic.semantic_search(ctx, q, config=ctx.config.search, limit=5)
                out.append(
                    {
                        "query": q,
                        "lexical": [
                            [r.kind, r.tier.name.lower(), attachment(r.location)] for r in lex
                        ],
                        "semantic": [[h.kind, attachment(h.location)] for h in sem.results],
                    }
                )
        finally:
            ctx.close()
    OUT.write_text(json.dumps({"queries": out}, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
