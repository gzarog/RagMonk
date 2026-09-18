"""``ragmonk status [--json]``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import (
    documents_repo,
    entities_repo,
    files_repo,
    jobs_repo,
    relationships_repo,
)
from ragmonk.tokenization import diagnostics

from ._common import cli_command, console, print_json


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _run(ctx: AppContext) -> dict[str, Any]:
    """Shared with Phase 6's ``ragmonk_status`` MCP tool.

    Metrics here are real, cheaply-obtainable numbers this codebase
    already tracks -- file/job counts (Phase 1), entity/relationship
    counts (Phase 2), document counts (Phase 3), database file size on
    disk. Deliberately excluded: query-latency style metrics (e.g. an
    average `explore`/`search` duration) -- nothing in this codebase
    times a query today, and inventing one only for a metrics field
    would be a fabricated number, not a real one. See CHANGELOG.md.
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = registry.list()

    per_source: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    total_queue_depth = 0
    total_symbols = 0
    total_relationships = 0
    total_documents = 0
    total_db_bytes = _file_size(paths.sources_db_path(ctx.home))

    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        conn = ctx.project_conn(project_id)
        counts = files_repo.count_by_status(conn, source.id)
        depth = jobs_repo.queue_depth(conn)
        total_queue_depth += depth
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value

        symbols = entities_repo.count_all(conn)
        relationships = relationships_repo.count_all(conn)
        documents = documents_repo.count_all(conn)
        db_bytes = _file_size(paths.project_db_path(project_id, ctx.home))
        total_symbols += symbols
        total_relationships += relationships
        total_documents += documents
        total_db_bytes += db_bytes

        per_source.append(
            {
                "id": source.id,
                "path": source.path,
                "enabled": source.enabled,
                "status": source.status.value,
                "counts": counts,
                "queue_depth": depth,
                "last_scan_at": source.last_scan_at,
                "last_error": source.last_error,
                "metrics": {
                    "symbols_created": symbols,
                    "relationships_created": relationships,
                    "documents_processed": documents,
                    "database_size_bytes": db_bytes,
                },
            }
        )

    # Exact Tokenizer plan, Phase 5: identify the pinned tokenizer the
    # active index is tied to. Cheap (no tokenizer load) -- see
    # ``tokenization.diagnostics.tokenizer_identity``.
    tokenizer = diagnostics.tokenizer_identity()
    tokenizer["chunk_ceiling"] = ctx.config.documents.chunking.resolved_max_tokens

    return {
        "sources": per_source,
        "tokenizer": tokenizer,
        "totals": {
            "by_status": totals,
            "queue_depth": total_queue_depth,
            "metrics": {
                "files_discovered": sum(totals.values()),
                "files_indexed": totals.get("indexed", 0),
                "files_failed": totals.get("failed", 0),
                "symbols_created": total_symbols,
                "relationships_created": total_relationships,
                "documents_processed": total_documents,
                "index_queue_depth": total_queue_depth,
                "database_size_bytes": total_db_bytes,
            },
        },
    }


@cli_command
def status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with AppContext.bootstrap() as ctx:
        data = _run(ctx)
        per_source = data["sources"]

        if json_output:
            print_json(data)
            return

        table = Table("Source", "Status", "Indexed", "Queued", "Failed", "Queue Depth", "Last Scan")
        for row in per_source:
            counts = row["counts"]
            table.add_row(
                row["id"],
                row["status"],
                str(counts.get("indexed", 0)),
                str(counts.get("queued", 0)),
                str(counts.get("failed", 0)),
                str(row["queue_depth"]),
                row["last_scan_at"] or "-",
            )
        console.print(table)

        metrics = data["totals"]["metrics"]
        console.print(
            f"Symbols: {metrics['symbols_created']}  "
            f"Relationships: {metrics['relationships_created']}  "
            f"Documents: {metrics['documents_processed']}  "
            f"DB size: {metrics['database_size_bytes'] / 1024:.1f} KB"
        )
