"""``ragmonk status [--json]``."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table

from ragmonk.core.lifecycle import AppContext
from ragmonk.service.status_service import collect_status

from ._common import cli_command, console, print_json


def _run(ctx: AppContext) -> dict[str, Any]:
    """Thin adapter kept for the ``ragmonk_status`` MCP tool.

    The aggregation itself lives in
    ``ragmonk.service.status_service.collect_status`` so the CLI, the MCP
    tool and the admin UI dashboard all read the exact same numbers (see
    that module and the Admin UI plan's Phase 12 service-consolidation
    goal).
    """
    return collect_status(ctx)


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
