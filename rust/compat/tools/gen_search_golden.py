"""Generate RUST-10 search golden fixtures from the Python reference.

    python rust/compat/tools/gen_search_golden.py

Indexes two corpora with semantic search on (local mode, temporary
RAGMONK_HOME):

* rust/compat/fixtures/search_quality/project with its 72 golden queries;
* rust/compat/fixtures/linking with rust/compat/fixtures/search/queries.json
  (edge cases: dotted aliases, headings, titles, path fragments, prefixes,
  punctuation, non-ASCII, empty-token queries).

For every query it records three ranked views, top 20, in an id-free form:

* ``lexical``: ``lexical.search`` -> [kind, tier, path, title].
* ``hybrid``: ``merger.merge`` + ``reranker.rerank`` over lexical (top 50)
  and semantic (top 50) -> [kind, tier_label, path, title].
* ``hybrid_neural``: the hybrid list with its top 20 rescored by the
  cross-encoder (``neural_reranker.rerank_hits``).

Paths are relative to the corpus root. The corpus is copied with every
mtime pinned to ``MTIME`` so recency tie-breaks are reproducible. Each hit
also carries its tie key: the reference's sort key without entity ids, with
semantic scores rounded to 5 decimals. Hits with equal tie keys
may legitimately appear in either order. Writes
rust/compat/golden/search.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import ann, fusion, lexical, merger, neural_reranker, reranker, semantic

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "golden" / "search.json"
TOP = 20
MTIME = 1_700_000_000


def _exact_usearch(self: ann.USearchAnnIndex, vector, k: int) -> list[tuple[int, float]]:
    """The production (usearch) engine, searched exactly. Its default HNSW
    search is approximate and can drop a candidate, which would make the
    golden order an artefact of one index build rather than of the ranking
    rules. (The reference's brute-force fallback is not used: unlike
    usearch it also drops negative similarities.)
    """
    import numpy as np

    if len(self._index) == 0:
        return []
    matches = self._index.search(np.array(vector, dtype=np.float32), k, exact=True)
    return [
        (int(key), 1.0 - float(distance))
        for key, distance in zip(matches.keys, matches.distances, strict=True)
    ]


ann.USearchAnnIndex.search = _exact_usearch
CORPORA = {
    "search_quality": (
        ROOT / "fixtures" / "search_quality" / "project",
        ROOT / "fixtures" / "search_quality" / "queries.json",
    ),
    "linking": (ROOT / "fixtures" / "linking", ROOT / "fixtures" / "search" / "queries.json"),
}


def queries_of(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))["queries"]
    return [q["query"] if isinstance(q, dict) else q for q in data]


def bm(x: float | None) -> float | None:
    return None if x is None else round(x, 5)


def lex_key(r: lexical.SearchResult) -> list:
    return [int(r.tier), int(r.query_tier), r.fts_rank, r.entity_kind_rank]


def hyb_key(h: reranker.RankedHit) -> list:
    c = h.candidate
    if c.exact_match:
        return [
            "pinned",
            int(c.lexical_tier),
            int(c.lexical_query_tier),
            c.lexical_fts_rank,
            c.entity_kind_rank,
            bm(c.semantic_score),
        ]
    return [
        "hybrid",
        round(c.rrf_score or 0.0, 9),
        int(c.lexical_query_tier),
        c.lexical_fts_rank,
        c.entity_kind_rank,
        bm(c.semantic_score),
    ]


def run_corpus(source: Path, queries: list[str]) -> list[dict]:
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as home:
        corpus = Path(home) / "corpus"
        shutil.copytree(source, corpus)
        for p in corpus.rglob("*"):
            os.utime(p, (MTIME, MTIME))
        env = {**os.environ, "RAGMONK_HOME": home, "RAGMONK_SEARCH__SEMANTIC": "true"}
        for args in (["init"], ["source", "add", str(corpus)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)
        os.environ["RAGMONK_SEARCH__SEMANTIC"] = "true"
        os.environ["RAGMONK_SEARCH__CACHE__ENABLED"] = "false"
        ctx = AppContext.bootstrap(home=Path(home), cwd=corpus.parent)
        root = corpus.resolve()

        def rel(path: str) -> str:
            return Path(path).resolve().relative_to(root).as_posix()

        out = []
        try:
            for q in queries:
                lex = lexical.search(ctx, q, limit=fusion.MAX_LEXICAL_CANDIDATES)
                sem = semantic.semantic_search(
                    ctx,
                    q,
                    config=ctx.config.search,
                    limit=fusion.MAX_SEMANTIC_CANDIDATES,
                    candidate_k=fusion.MAX_SEMANTIC_CANDIDATES,
                )
                ranked = reranker.rerank(merger.merge(lex, list(sem.results)), limit=TOP)
                neural = neural_reranker.rerank_hits(q, ranked, top_n=TOP)

                def title(kind: str, path: str, title: str) -> str:
                    return rel(path) if kind == "path" else title

                def hit(h: reranker.RankedHit) -> list:
                    c = h.candidate
                    t = title(c.kind, c.path, c.title)
                    return [c.kind, h.tier_label, rel(c.path), t, hyb_key(h)]

                out.append(
                    {
                        "query": q,
                        "lexical": [
                            [
                                r.kind,
                                r.tier.name.lower(),
                                rel(r.path),
                                title(r.kind, r.path, r.title),
                                lex_key(r),
                            ]
                            for r in lex[:TOP]
                        ],
                        "hybrid": [hit(h) for h in ranked],
                        "hybrid_neural": [hit(h)[:4] for h in neural],
                    }
                )
        finally:
            ctx.close()
        return out


def main() -> None:
    payload = {
        name: run_corpus(corpus, queries_of(qpath)) for name, (corpus, qpath) in CORPORA.items()
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: " + ", ".join(f"{k}={len(v)}" for k, v in payload.items()))


if __name__ == "__main__":
    main()
