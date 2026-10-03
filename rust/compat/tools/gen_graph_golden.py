"""Generate RUST-10 symbol/graph golden fixtures from the Python reference.

    python rust/compat/tools/gen_graph_golden.py

Indexes three corpora (local mode, temporary RAGMONK_HOME):
rust/compat/fixtures/graph (chains, a cycle, unresolved names, test files),
rust/compat/fixtures/code and rust/compat/fixtures/linking.

The queries are every entity name and qualified name, plus names that only
appear as unresolved call targets and a name that exists nowhere. For each
query the generator records, at max depth 1 and 3:

* ``symbol``: the matches;
* ``callers`` / ``callees``: ``traverse_symbol`` over CALLS;
* ``references``: ``retrieval.graph.references``;
* ``incoming`` / ``outgoing``: resolved CALLS/IMPORTS/REFERENCES edges with
  the neighbour entity;
* ``tests``: ``find_tests_referencing``.

After indexing, the reference's relationships are rebuilt against the whole
corpus (see ``reresolve``), so they do not depend on scan order. Everything
is id-free:

* an entity is ``[path, qualified_name, kind, start_line]``;
* an edge is ``[depth, type, source entity, target entity or
  ["symbol", name], confidence, resolver]``.

Edge order within one depth follows entity ids, which differ between
implementations, so consumers compare each depth as a multiset.
Writes rust/compat/golden/graph.json.
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

from ragmonk.code import graph as code_graph
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import RelationshipType
from ragmonk.retrieval import graph

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "golden" / "graph.json"
CORPORA = ("graph", "code", "linking")
EXTRA = ["audit_log", "external_hook", "trackEvent", "no_such_symbol_anywhere"]
CALLS = (RelationshipType.CALLS,)


def reresolve(db: Path, root: Path) -> None:
    """Rebuilds every file's relationships with the reference's own
    ``_build_relationships`` against the whole corpus.

    The reference resolves a reference against the entities stored when its
    file is processed, so the stored graph depends on scan order. Re-running
    it, or re-indexing, never converges: re-parsing a file briefly removes its
    entities. V2 resolves against the complete build (ADR 0009), and the
    RUST-05 golden established that the rules then agree. Recomputing the
    edges this way gives the reference the same whole-corpus graph, so the
    golden checks the query logic, not the processing order.
    """
    from pathlib import PurePosixPath
    from types import SimpleNamespace

    from ragmonk.code import processor
    from ragmonk.code.extractor import default_namespace_for_path, extract
    from ragmonk.code.parser import detect_language, parse
    from ragmonk.storage.repositories import entities_repo, relationships_repo

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    every = entities_repo.list_all(conn)
    for file_id, path in conn.execute("SELECT id, path FROM files").fetchall():
        language = detect_language(Path(path))
        if language is None:
            continue
        source = Path(path).read_bytes()
        if parse(source, language).root_node.has_error:
            continue
        rel = PurePosixPath(Path(path).relative_to(root).as_posix())
        default_name, default_qualified = default_namespace_for_path(rel)
        extraction = extract(
            source,
            language,
            default_namespace_name=default_name,
            default_namespace_qualified_name=default_qualified,
        )
        stored = {
            (e.qualified_name, e.kind.value, e.start_line, e.start_col): e
            for e in entities_repo.list_by_file(conn, file_id)
        }
        entities = [
            stored[(x.qualified_name, x.kind.value, x.start_line, x.start_col)]
            for x in extraction.entities
        ]
        ns_local = next(i for i, x in enumerate(extraction.entities) if x.kind.value == "namespace")

        def qualified_lookup(text: str, fid: str = file_id) -> list:
            return [e for e in every if e.file_id != fid and e.qualified_name == text]

        def name_lookup(name: str, fid: str = file_id) -> list:
            return [e for e in every if e.file_id != fid and e.name == name]

        ctx = SimpleNamespace(file_id=file_id, next_generation=entities[0].generation, path=path)
        rels = processor._build_relationships(
            ctx=ctx,
            extraction=extraction,
            entities=entities,
            namespace_local_id=ns_local,
            language=language,
            now=entities[0].created_at,
            qualified_lookup=qualified_lookup,
            name_lookup=name_lookup,
        )
        conn.execute("DELETE FROM relationships WHERE file_id = ?", (file_id,))
        for r in rels:
            relationships_repo.insert(conn, r)
    conn.commit()
    conn.close()


def run_corpus(name: str) -> dict:
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as home:
        corpus = Path(home) / "corpus"
        shutil.copytree(ROOT / "fixtures" / name, corpus)
        env = {**os.environ, "RAGMONK_HOME": home}
        for args in (["init"], ["source", "add", str(corpus)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)
        (db,) = Path(home, "projects").glob("*/knowledge.db")
        reresolve(db, corpus.resolve())
        conn = sqlite3.connect(db)
        root = corpus.resolve()

        def rel(path: str) -> str:
            return os.path.relpath(path, root).replace(os.sep, "/")

        ent = {
            eid: [rel(path), qn, kind, line]
            for eid, path, qn, kind, line in conn.execute(
                "SELECT e.id, f.path, e.qualified_name, e.kind, e.start_line "
                "FROM entities e JOIN files f ON f.id = e.file_id"
            )
        }
        dangling = conn.execute(
            "SELECT count(*) FROM relationships r WHERE r.target_entity_id IS NOT NULL "
            "AND r.target_entity_id NOT IN (SELECT id FROM entities)"
        ).fetchone()[0]
        assert dangling == 0, f"{name}: {dangling} dangling edges"
        names = sorted(
            {n for (n,) in conn.execute("SELECT name FROM entities")}
            | {n for (n,) in conn.execute("SELECT qualified_name FROM entities")}
        )
        queries = names + EXTRA

        def edge(e: code_graph.TraversalEdge) -> list:
            r = e.relationship
            target = (
                ent.get(r.target_entity_id) if r.target_entity_id else ["symbol", r.target_symbol]
            )
            return [
                e.depth,
                r.relationship_type.value,
                ent.get(r.source_entity_id),
                target,
                r.confidence.value,
                r.resolver,
            ]

        def resolved(r: graph.ResolvedEdge) -> list:
            n = r.neighbor_entity
            return [*edge(r.edge), ent.get(n.id) if n else None]

        os.environ["RAGMONK_HOME"] = home
        ctx = AppContext.bootstrap(home=Path(home), cwd=corpus.parent)
        out = []
        try:
            for q in queries:
                item: dict = {"query": q}
                for depth in (1, 3):
                    _, callers = code_graph.traverse_symbol(
                        ctx, q, direction="incoming", relationship_types=CALLS, max_depth=depth
                    )
                    _, callees = code_graph.traverse_symbol(
                        ctx, q, direction="outgoing", relationship_types=CALLS, max_depth=depth
                    )
                    matches, refs = graph.references(ctx, q, max_depth=depth)
                    inc = graph.resolved_incoming(
                        ctx, matches, q, relationship_types=graph.REFERENCE_TYPES, max_depth=depth
                    )
                    outg = graph.resolved_outgoing(
                        ctx, matches, relationship_types=graph.REFERENCE_TYPES, max_depth=depth
                    )
                    tests = graph.find_tests_referencing(ctx, matches, q, max_depth=depth)
                    item[f"d{depth}"] = {
                        "callers": [edge(e) for e in callers],
                        "callees": [edge(e) for e in callees],
                        "references": [edge(e) for e in refs],
                        "incoming": [resolved(r) for r in inc],
                        "outgoing": [resolved(r) for r in outg],
                        "tests": [resolved(r) for r in tests],
                    }
                item["symbol"] = [ent[m.entity.id] for m in matches]
                out.append(item)
        finally:
            ctx.close()
        return {"queries": out}


def main() -> None:
    payload = {name: run_corpus(name) for name in CORPORA}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT}: " + ", ".join(f"{k}={len(v['queries'])} queries" for k, v in payload.items())
    )


if __name__ == "__main__":
    main()
