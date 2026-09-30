"""``ragmonk status [--json] [--watch] [--errors] [--verbose]``."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import typer
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service.status_service import collect_status

from ._common import cli_command, console, print_json

_HEALTH_STYLE = {"healthy": "green", "degraded": "yellow", "failed": "red"}
_INDEXER_STYLE = {"running": "green", "idle": "dim", "stalled": "red", "crashed": "red"}
_INDEX_STATE_STYLE = {
    "indexing": "green",
    "completed": "green",
    "waiting": "cyan",
    "retrying": "yellow",
    "errors": "yellow",
    "stalled": "red",
    "offline": "red",
    "idle": "dim",
}
_SEVERITY_STYLE = {"error": "red", "warning": "yellow", "info": "dim"}


def _run(ctx: AppContext) -> dict[str, Any]:
    """Thin adapter kept for the ``ragmonk_status`` MCP tool.

    The aggregation itself lives in
    ``ragmonk.service.status_service.collect_status`` so the CLI, the MCP
    tool and the admin UI dashboard all read the exact same numbers (see
    that module and the Admin UI plan's Phase 12 service-consolidation
    goal).
    """
    return collect_status(ctx)


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def _fmt_since(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _fmt_age(max(0.0, (datetime.now(UTC) - parsed).total_seconds()))


def _styled(value: str, styles: dict[str, str]) -> Text:
    return Text(value, style=styles.get(value, ""))


@dataclass
class _Sample:
    at: float
    indexed: int
    failed: int
    retry: int
    depth: int


def _sample(data: dict[str, Any]) -> _Sample:
    queue = data.get("queue", {})
    by_status = data["totals"]["by_status"]
    return _Sample(
        at=time.monotonic(),
        indexed=int(by_status.get("indexed", 0)),
        failed=int(by_status.get("failed", 0)),
        retry=int(queue.get("retry", 0)),
        depth=int(queue.get("depth", 0)),
    )


def watch_deltas(previous: _Sample | None, current: _Sample) -> dict[str, Any]:
    """Presentation-time deltas between two consecutive samples. Never
    persisted. Throughput is ``n/a`` until two samples exist, and a
    counter reset (rebuild) never produces a negative rate.
    """
    if previous is None:
        return {"indexed": None, "failed": None, "retry": None, "depth": None, "rate": None}
    elapsed = current.at - previous.at
    d_indexed = current.indexed - previous.indexed
    rate = d_indexed / elapsed if elapsed > 0 and d_indexed >= 0 else None
    return {
        "indexed": d_indexed,
        "failed": current.failed - previous.failed,
        "retry": current.retry - previous.retry,
        "depth": current.depth - previous.depth,
        "rate": rate,
    }


def _signed(value: int | None) -> str:
    return "n/a" if value is None else f"{value:+d}"


def _indexer_panel(data: dict[str, Any], *, verbose: bool) -> Panel:
    indexer = data["indexer"]
    state = indexer["state"]
    lines = Text()
    lines.append("State: ")
    lines.append(state, style=_INDEXER_STYLE.get(state, ""))
    if indexer.get("stall_reason"):
        lines.append(f"  ({indexer['stall_reason']})", style="red")
    if state in ("running", "stalled"):
        lines.append(
            f"\nPID: {indexer.get('pid') or '-'}  Operation: {indexer.get('operation') or '-'}"
            f"  Running for: {_fmt_duration(indexer.get('running_for_seconds'))}"
        )
        position = indexer.get("source_position")
        total = indexer.get("source_total")
        where = f" ({position}/{total})" if position and total else ""
        lines.append(
            f"\nSource: {indexer.get('current_source_id') or '-'}{where}"
            f"  Stage: {indexer.get('stage') or '-'}"
            f"  Last activity: {_fmt_age(indexer.get('last_activity_age_seconds'))}"
        )
        run = indexer.get("run") or {}
        if run:
            lines.append(
                f"\nThis run: scanned={run.get('scanned', 0)} indexed={run.get('indexed', 0)} "
                f"retry={run.get('retry', 0)} failed={run.get('failed', 0)}"
            )
    last_run = indexer.get("last_run")
    if last_run and state not in ("running", "stalled"):
        outcome = last_run.get("outcome")
        lines.append(
            f"\nLast run: {last_run.get('operation') or '-'} {outcome}"
            f" at {last_run.get('completed_at') or last_run.get('updated_at') or '-'}"
        )
        if last_run.get("error"):
            lines.append(f"\n  {last_run['error']}", style="red")
    if verbose:
        lines.append(
            f"\nLock: {indexer.get('lock_state')}"
            f"  owner pid={indexer.get('pid') or '-'}"
            f" host={indexer.get('hostname') or '-'}"
            f" source={indexer.get('lock_source_id') or '-'}"
            f" acquired={indexer.get('acquired_at') or '-'}"
        )
        lines.append(
            f"\nProgress: started={indexer.get('started_at') or '-'}"
            f" last_activity={indexer.get('last_activity_at') or '-'}"
            f" stall_threshold={indexer.get('stall_threshold_seconds')}s"
        )
    return Panel(lines, title="Indexer", expand=False)


def _summary_panel(data: dict[str, Any], *, verbose: bool, deltas: dict[str, Any] | None) -> Panel:
    health = data["health"]["status"]
    queue = data["queue"]
    metrics = data["totals"]["metrics"]
    text = Text()
    text.append("Health: ")
    text.append(health, style=_HEALTH_STYLE.get(health, ""))
    text.append(f"  ({data['health']['problem_count']} problem(s))")
    text.append(
        f"\nFiles: indexed={metrics['files_indexed']} failed={metrics['files_failed']}"
        f" discovered={metrics['files_discovered']}"
    )
    text.append(
        f"\nJobs: queued={queue['queued']} processing={queue['processing']}"
        f" retry={queue['retry']} failed={queue['failed']}"
    )
    if deltas is not None:
        rate = deltas["rate"]
        text.append(
            f"\nDelta: indexed={_signed(deltas['indexed'])} failed={_signed(deltas['failed'])}"
            f" retry={_signed(deltas['retry'])} queue={_signed(deltas['depth'])}"
            f"  Throughput: {'n/a' if rate is None else f'{rate:.1f} files/s'}"
        )
    if verbose:
        text.append(
            f"\nOldest pending: {queue.get('oldest_pending_created_at') or '-'}"
            f"  Next retry: {queue.get('next_retry_at') or '-'}"
            f"  Max attempts: {queue.get('max_attempt_count', 0)}"
        )
        text.append(
            f"\nSymbols: {metrics['symbols_created']}  "
            f"Relationships: {metrics['relationships_created']}  "
            f"Documents: {metrics['documents_processed']}  "
            f"DB size: {metrics['database_size_bytes'] / 1024:.1f} KB"
        )
    backend = data.get("backend", {})
    text.append(f"\nBackend: {backend.get('type', 'local')}")
    if backend.get("type", "local") != "local":
        if "error" in backend:
            text.append(f"  {backend['error']}", style="red")
        else:
            counts = backend.get("counts", {})
            text.append(
                f"  files={counts.get('files', 0)} entities={counts.get('entities', 0)}"
                f" document_units={counts.get('document_units', 0)}"
                f" embeddings={counts.get('embeddings', 0)}"
            )
    return Panel(text, title="Summary", expand=False)


def _sources_table(rows: list[dict[str, Any]], *, verbose: bool) -> Table:
    columns = [
        "Source",
        "Access",
        "Index State",
        "Indexed",
        "Queued",
        "Processing",
        "Retry",
        "Failed",
        "Last Activity",
    ]
    if verbose:
        # "Last Scan" = filesystem scan completion, not end-to-end indexing.
        columns.append("Last Scan (completed)")
    table = Table(*columns, title="Sources")
    for row in rows:
        counts = row["counts"]
        queue = row.get("queue", {})
        table.add_row(
            row["id"],
            _styled(row.get("access_state", row["status"]), {"offline": "red"}),
            _styled(row.get("index_state", "-"), _INDEX_STATE_STYLE),
            str(counts.get("indexed", 0)),
            str(queue.get("queued", 0)),
            str(queue.get("processing", 0)),
            str(queue.get("retry", 0)),
            str(counts.get("failed", 0)),
            _fmt_since(row.get("last_activity_at")),
            *([row["last_scan_at"] or "-"] if verbose else []),
        )
        if row.get("last_error") and (verbose or row.get("index_state") != "completed"):
            table.add_row("", Text(f"last error: {row['last_error']}", style="red"))
        detail = row.get("last_error_detail")
        if verbose and detail:
            table.add_row(
                "",
                Text(
                    f"last file error: {detail.get('path')}: "
                    f"{detail.get('error_code')}: {detail.get('error_message')}",
                    style="yellow",
                ),
            )
    return table


def _errors_section(
    data: dict[str, Any], *, limit: int, include_info: bool
) -> RenderableType | None:
    problems = [p for p in data["problems"] if include_info or p["severity"] != "info"]
    errors = data["recent_errors"][:limit]
    if not problems and not errors:
        return None
    text = Text()
    for problem in problems:
        text.append(f"[{problem['severity']}] ", style=_SEVERITY_STYLE.get(problem["severity"]))
        scope = problem["scope"] + (f" {problem['source_id']}" if problem.get("source_id") else "")
        text.append(f"{scope}: {problem['message']}\n")
    if errors:
        text.append("Recent errors:\n", style="bold")
        for error in errors:
            text.append(
                f"  {error.get('occurred_at') or '-'} {error.get('source_id')} "
                f"{error.get('path') or '-'}: {error.get('error_code')}: "
                f"{error.get('error_message')}\n"
            )
    text.rstrip()
    return Panel(text, title="Problems", expand=False)


def render_status(
    data: dict[str, Any],
    *,
    errors_only: bool = False,
    verbose: bool = False,
    deltas: dict[str, Any] | None = None,
) -> RenderableType:
    parts: list[RenderableType] = [_indexer_panel(data, verbose=verbose)]
    parts.append(_summary_panel(data, verbose=verbose, deltas=deltas))
    rows = data["sources"]
    if errors_only:
        rows = [
            row
            for row in rows
            if row.get("index_state") in ("errors", "retrying", "offline", "stalled")
            or row.get("last_error")
        ]
    if rows or not errors_only:
        parts.append(_sources_table(rows, verbose=verbose))
    detailed = verbose or errors_only
    errors = _errors_section(data, limit=10 if detailed else 3, include_info=detailed)
    if errors is not None:
        parts.append(errors)
    elif errors_only:
        parts.append(Text("No problems found.", style="green"))
    return Group(*parts)


def _collect() -> dict[str, Any]:
    # Fresh bootstrap per read: connections never outlive one refresh.
    with AppContext.bootstrap() as ctx:
        return _run(ctx)


def _watch(*, interval: float, errors_only: bool, verbose: bool) -> None:
    from rich.live import Live

    previous: _Sample | None = None
    try:
        with Live(console=console, auto_refresh=False, transient=False) as live:
            while True:
                data = _collect()
                current = _sample(data)
                deltas = watch_deltas(previous, current)
                previous = current
                live.update(
                    render_status(data, errors_only=errors_only, verbose=verbose, deltas=deltas),
                    refresh=True,
                )
                time.sleep(interval)
    except KeyboardInterrupt:
        return


@cli_command
def status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable output (full status model).")
    ] = False,
    watch: Annotated[
        bool, typer.Option("--watch", help="Refresh continuously with deltas; Ctrl+C to exit.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", help="Seconds between --watch refreshes.")
    ] = 2.0,
    errors: Annotated[
        bool, typer.Option("--errors", help="Only problematic sources and recent errors.")
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Lock, progress, queue and error details."),
    ] = False,
) -> None:
    """Show indexing status: is indexing running, progressing, stalled or
    failing, per-source access vs. index state, and recent errors.
    """
    if watch and json_output:
        raise UsageError("--watch cannot be combined with --json")
    if interval <= 0:
        raise UsageError("--interval must be > 0")
    if watch:
        _watch(interval=interval, errors_only=errors, verbose=verbose)
        return

    data = _collect()
    if json_output:
        print_json(data)
        return
    console.print(render_status(data, errors_only=errors, verbose=verbose))
