"""Generate RUST-10 explore/impact golden fixtures from the Python reference.

    python rust/compat/tools/gen_explore_golden.py

Indexes rust/compat/fixtures/linking (code + documents + cross-domain links)
and rust/compat/fixtures/graph (call chains, tests), local mode, semantic
search off, in a temporary RAGMONK_HOME. Relationships are rebuilt against
the whole corpus (see gen_graph_golden.reresolve).

It records:

* ``impact``: ``cli.impact._run`` for every entity name and qualified name,
  plus a missing name;
* ``explore``: ``cli.explore._run(planner.plan(q))`` for a fixed set of
  planner-shaped queries.

Cross-domain links are read strongest first (see ``_pin_link_order``).
Paths are made corpus-relative and entity/source ids are dropped, so the
payloads compare across implementations. Writes
rust/compat/golden/explore.json.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_graph_golden import reresolve  # noqa: E402

from ragmonk.cli import explore as explore_cli  # noqa: E402
from ragmonk.cli import impact as impact_cli  # noqa: E402
from ragmonk.core.lifecycle import AppContext  # noqa: E402
from ragmonk.retrieval import planner  # noqa: E402

_CONFIDENCE = {"exact": 0, "high": 1, "medium": 2}
_RESOLVERS = [
    "linker:exact_identifier",
    "linker:qualified_identifier",
    "linker:alias",
    "linker:filename",
    "linker:route_heuristic",
]


def _pin_link_order() -> None:
    """``links_repo.list_by_entity`` orders by creation time, then random
    id, so which link wins a (document, section) in documentation evidence
    is arbitrary among links created in the same instant. V2 orders by
    confidence, the linker's resolver order, then link type. The generator
    applies the same order to the reference.
    """
    from ragmonk.knowledge import document_links
    from ragmonk.storage.repositories import links_repo

    original = links_repo.list_by_entity

    def ordered(conn, entity_id):
        def key(link):
            resolver = _RESOLVERS.index(link.resolver) if link.resolver in _RESOLVERS else 5
            return (_CONFIDENCE.get(link.confidence.value, 3), resolver, link.link_type.value)

        return sorted(original(conn, entity_id), key=key)

    links_repo.list_by_entity = ordered
    assert document_links.links_repo is links_repo


_pin_link_order()

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "golden" / "explore.json"
EXPLORE = {
    "linking": [
        "InvoiceService",
        "void_invoice",
        "src.billing.invoice_service.InvoiceService.void_invoice",
        "who calls void_invoice?",
        "what does InvoiceService.void_invoice call",
        "what breaks if Ledger changes?",
        "impact of OrderController",
        "documents about refunds",
        "billing documentation",
        "how are invoices voided",
        "cancel order",
        "Λογαριασμός",
        "no_such_thing_anywhere",
    ],
    "graph": [
        "who calls normalize",
        "callers of settle",
        "what does settle call",
        "what breaks if normalize changes?",
        "what depends on Ledger.post",
        "Settle",
        "ledger post amount",
    ],
}


def strip(value, root: str):
    """Relativizes paths and drops implementation-specific ids."""
    if isinstance(value, dict):
        return {
            k: strip(v, root)
            for k, v in value.items()
            if k not in ("entity_id", "source_id", "id", "parent_document_id")
        }
    if isinstance(value, list):
        return [strip(v, root) for v in value]
    if isinstance(value, str) and value.startswith(root + os.sep):
        return os.path.relpath(value, root).replace(os.sep, "/")
    return value


def run_corpus(name: str) -> dict:
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as home:
        corpus = Path(home) / "corpus"
        shutil.copytree(ROOT / "fixtures" / name, corpus)
        env = {**os.environ, "RAGMONK_HOME": home, "RAGMONK_SEARCH__SEMANTIC": "false"}
        for args in (["init"], ["source", "add", str(corpus)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)
        (db,) = Path(home, "projects").glob("*/knowledge.db")
        reresolve(db, corpus.resolve())
        conn = sqlite3.connect(db)
        names = sorted(
            {n for (n,) in conn.execute("SELECT name FROM entities")}
            | {n for (n,) in conn.execute("SELECT qualified_name FROM entities")}
        )
        conn.close()
        os.environ["RAGMONK_HOME"] = home
        os.environ["RAGMONK_SEARCH__SEMANTIC"] = "false"
        os.environ["RAGMONK_SEARCH__CACHE__ENABLED"] = "false"
        ctx = AppContext.bootstrap(home=Path(home), cwd=corpus.parent)
        root = str(corpus.resolve())
        try:
            impact = []
            for q in [*names, "no_such_symbol_anywhere"]:
                for d in (1, 3):
                    item = strip(impact_cli._run(ctx, q, max_depth=d, limit=100), root)
                    # Same-named matches are ordered by random entity ids in
                    # the reference; V2 orders them by path and line.
                    if "defined" in item:
                        item["defined"].sort(
                            key=lambda e: (e["qualified_name"], e["path"], e["start_line"])
                        )
                    impact.append(item | {"max_depth": d})
            explore = [
                strip(explore_cli._run(ctx, planner.plan(q, semantic_enabled=False)), root)
                for q in EXPLORE[name]
            ]
        finally:
            ctx.close()
        return {"impact": impact, "explore": explore}


def main() -> None:
    payload = {name: run_corpus(name) for name in EXPLORE}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT}: "
        + ", ".join(
            f"{k}: {len(v['impact'])} impact, {len(v['explore'])} explore"
            for k, v in payload.items()
        )
    )


if __name__ == "__main__":
    main()
