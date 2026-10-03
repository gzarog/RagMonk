"""Generate the RUST-09 semantic search-quality baseline from the reference.

    python rust/compat/tools/gen_quality_golden.py

Writes the reference search-quality fixture project
(``benchmarks/search_quality/fixture_project.py``) to
``rust/compat/fixtures/search_quality/project`` and its 72 golden queries to
``.../queries.json``. It then indexes the project with semantic search on
and scores two arms over every query:

* ``semantic``: ``semantic.semantic_search`` top 10.
* ``semantic_rerank``: the top 20 semantic candidates rescored by the
  reference cross-encoder (``neural_reranker.rerank_hits`` contract:
  snippet, else title), then top 10.

Metrics are the reference's own (Recall@1/3/5/10, MRR, NDCG@10 over
``kind:path`` keys). Writes ``rust/compat/benchmarks/python-semantic-quality.json``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from benchmarks.search.quality import (  # noqa: E402
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
    result_key,
)
from benchmarks.search_quality.evaluator import load_golden_queries  # noqa: E402
from benchmarks.search_quality.fixture_project import write_project  # noqa: E402

from ragmonk.core.lifecycle import AppContext  # noqa: E402
from ragmonk.retrieval import neural_reranker, semantic  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "search_quality"
OUT = ROOT / "benchmarks" / "python-semantic-quality.json"
TOP_N = 20
K_VALUES = (1, 3, 5, 10)


def metrics(ranked: dict[str, list[str]], golden: list[dict]) -> dict:
    per = []
    for item in golden:
        got = ranked[item["query"]]
        rel = {result_key(e["kind"], e["path"]) for e in item["expected"]}
        per.append(
            {
                **{f"recall@{k}": recall_at_k(got, rel, k) for k in K_VALUES},
                "mrr": reciprocal_rank(got, rel),
                "ndcg@10": ndcg_at_k(got, rel, 10),
            }
        )
    return {key: round(sum(p[key] for p in per) / len(per), 4) for key in per[0]}


def main() -> None:
    golden = load_golden_queries()
    project = FIXTURE / "project"
    shutil.rmtree(project, ignore_errors=True)
    write_project(project)
    (FIXTURE / "queries.json").write_text(
        json.dumps({"queries": golden}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    exe = Path(sys.executable).with_name("ragmonk")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        shutil.copytree(project, root)
        home = Path(tmp) / "home"
        env = {**os.environ, "RAGMONK_HOME": str(home), "RAGMONK_SEARCH__SEMANTIC": "true"}

        for args in (["init"], ["source", "add", str(root)], ["index"]):
            subprocess.run([str(exe), *args], env=env, check=True, capture_output=True)
        os.environ["RAGMONK_SEARCH__SEMANTIC"] = "true"
        ctx = AppContext.bootstrap(home=home, cwd=root.parent)
        try:
            arms: dict[str, dict[str, list[str]]] = {"semantic": {}, "semantic_rerank": {}}
            for item in golden:
                q = item["query"]
                hits = list(
                    semantic.semantic_search(
                        ctx, q, config=ctx.config.search, limit=TOP_N, candidate_k=50
                    ).results
                )

                def key(h: semantic.SemanticHit) -> str:
                    return result_key(h.kind, Path(h.path).relative_to(root).as_posix())

                arms["semantic"][q] = [key(h) for h in hits[:10]]
                scores = neural_reranker.score_pairs(q, [h.snippet or h.title for h in hits])
                order = sorted(range(len(hits)), key=lambda i: -scores[i])
                arms["semantic_rerank"][q] = [key(hits[i]) for i in order][:10]
        finally:
            ctx.close()
    payload = {
        "phase": "RUST-09",
        "implementation": "python",
        "queries": len(golden),
        "rerank_top_n": TOP_N,
        "semantic": metrics(arms["semantic"], golden),
        "semantic_rerank": metrics(arms["semantic_rerank"], golden),
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
