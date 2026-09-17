"""``ragmonk vectors rebuild [--source ID] [--json]`` (blueprint section
15): forces a full ANN index rebuild from ``vector_items``/``embeddings``
-- SQLite's own always-authoritative state -- rather than waiting on the
automatic rebuild-on-fragmentation policy ``retrieval/ann.sync_index_for_files``
already applies during normal indexing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from ragmonk.code.graph import all_project_connections
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import ann, embedder
from ragmonk.storage.repositories import embeddings_repo, vector_items_repo

from ._common import cli_command, console, print_json

app = typer.Typer(no_args_is_help=True, help="Manage the semantic-search ANN index.")


@app.command("rebuild")
@cli_command
def rebuild(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only rebuild this source id.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        model_id = embedder.EMBEDDING_MODEL_ID
        engine = ctx.config.search.vector.engine
        rebuilt: list[dict[str, Any]] = []
        for sid, source_path, conn in all_project_connections(ctx):
            if source_id is not None and sid != source_id:
                continue
            project_id = paths.project_id_for_path(Path(source_path))
            dim = embeddings_repo.get_dim_for_model(conn, model_id)
            if dim is None:
                rebuilt.append({"source_id": sid, "backend": None, "vectors": 0})
                continue
            backend = ann.rebuild_index(
                conn,
                project_id=project_id,
                home=ctx.home,
                engine=engine,
                ndim=dim,
                model_id=model_id,
            )
            rebuilt.append(
                {
                    "source_id": sid,
                    "backend": backend,
                    "vectors": vector_items_repo.count_all(conn, model_id=model_id),
                }
            )

    if json_output:
        print_json({"rebuilt": rebuilt})
        return

    if not rebuilt:
        console.print("[yellow]No sources to rebuild.[/yellow]")
        return
    for entry in rebuilt:
        if entry["backend"] is None:
            console.print(f"[dim]{entry['source_id']}: no embeddings computed yet.[/dim]")
        else:
            console.print(
                f"[bold]{entry['source_id']}[/bold]: rebuilt {entry['vectors']} vector(s) "
                f"via {entry['backend']}"
            )
