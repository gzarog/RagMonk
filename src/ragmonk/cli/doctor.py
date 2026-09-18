"""``ragmonk doctor [--json]`` and ``ragmonk health [--json]``."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from ragmonk import __version__
from ragmonk.core import paths
from ragmonk.core.errors import EXIT_HEALTH_CHECK_FAILURE
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import SourceStatus
from ragmonk.sources.registry import SourceRegistry
from ragmonk.sources.scanner import check_root_accessible
from ragmonk.storage import schema
from ragmonk.storage.migrations import current_version
from ragmonk.storage.repositories import documents_repo, jobs_repo, vector_items_repo
from ragmonk.tokenization import diagnostics
from ragmonk.tokenization.model_tokenizer import TokenizerAssetError

from ._common import cli_command, console, print_json

Status = Literal["ok", "warn", "fail"]

_LOW_DISK_WARN_GB = 1.0
_QUEUE_DEPTH_WARN = 1000


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str


@dataclass
class CheckSection:
    name: str
    checks: list[CheckResult]


def overall_status(sections: list[CheckSection]) -> str:
    """Public: reused by ``ragmonk upgrade`` (``ops/upgrade.py``) to run
    the exact same post-upgrade health verdict as ``ragmonk doctor``
    rather than reimplementing it.
    """
    statuses = {check.status for section in sections for check in section.checks}
    if "fail" in statuses:
        return "UNHEALTHY"
    if "warn" in statuses:
        return "HEALTHY WITH WARNINGS"
    return "HEALTHY"


def run_checks(ctx: AppContext) -> list[CheckSection]:
    sections: list[CheckSection] = []

    sections.append(
        CheckSection("Core", [CheckResult("version", "ok", f"version {__version__}")])
    )

    journal_mode = ctx.sources_conn.execute("PRAGMA journal_mode").fetchone()[0]
    schema_version = current_version(ctx.sources_conn)
    db_checks = [
        CheckResult("sqlite", "ok", "SQLite"),
        CheckResult(
            "wal", "ok" if journal_mode == "wal" else "warn", f"WAL ({journal_mode})"
        ),
        CheckResult(
            "schema",
            "ok" if schema_version == schema.CURRENT_SCHEMA_VERSION else "fail",
            f"schema v{schema_version}",
        ),
    ]
    sections.append(CheckSection("Database", db_checks))

    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = registry.list(enabled_only=True)
    # A live check here, not just each source's persisted ``status`` --
    # ``doctor`` must report a source that just went offline even before
    # the next ``ragmonk index``/daemon pass has had a chance to record
    # that. Either way this is only ever a WARN, never a FAIL: an
    # unreachable source (an unmounted network share, most commonly) is
    # exactly the transient condition Phase 7's offline/online handling
    # exists to tolerate without treating it as data loss -- see
    # ``indexing/coordinator.py``.
    unreachable = [
        s
        for s in sources
        if s.status is SourceStatus.OFFLINE or check_root_accessible(Path(s.path)) is not None
    ]
    sources_status: Status = "warn" if unreachable else "ok"
    sections.append(
        CheckSection(
            "Sources",
            [
                CheckResult(
                    "reachability",
                    sources_status,
                    f"{len(sources) - len(unreachable)}/{len(sources)} reachable",
                )
            ],
        )
    )

    total_queue = 0
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        conn = ctx.project_conn(project_id)
        total_queue += jobs_repo.queue_depth(conn)
    queue_status: Status = "ok" if total_queue < _QUEUE_DEPTH_WARN else "warn"
    detail = "queue empty" if total_queue == 0 else f"queue depth {total_queue}"
    sections.append(CheckSection("Index", [CheckResult("queue", queue_status, detail)]))

    free_bytes = shutil.disk_usage(ctx.home).free
    free_gb = free_bytes / (1024**3)
    disk_status: Status = "ok" if free_gb >= _LOW_DISK_WARN_GB else "warn"
    sections.append(
        CheckSection("Disk", [CheckResult("free_space", disk_status, f"{free_gb:.0f} GB free")])
    )

    # Search performance redesign, blueprint section 32: report which
    # semantic-search ANN backend is actually resolvable and how many
    # vectors/how much on-disk index size every source currently has --
    # never a warn/fail on its own (an empty or bruteforce-fallback state
    # is a normal, working configuration, not a health problem).
    if ctx.config.search.semantic:
        sections.append(CheckSection("Semantic", [_semantic_check(ctx, sources)]))

    sections.append(_tokenizer_section(ctx, sources))
    sections.append(_ai_section(ctx))

    return sections


def _tokenizer_section(ctx: AppContext, sources: list[Any]) -> CheckSection:
    """Exact Tokenizer plan, Phase 5: report which pinned tokenizer the
    active index is tied to, the effective chunk ceiling, and -- by
    measuring every stored embedding payload against the model's real
    input limit -- prove no payload is silently truncated (the truncation
    count must remain zero).
    """
    identity = diagnostics.tokenizer_identity()
    ceiling = ctx.config.documents.chunking.resolved_max_tokens
    checks: list[CheckResult] = [
        CheckResult("model", "ok", f"{identity['model_id']}"),
        CheckResult("revision", "ok", f"revision {identity['revision'][:12]}"),
        CheckResult("fingerprint", "ok", identity["fingerprint"][:19]),
        CheckResult(
            "limits",
            "ok",
            f"model max {identity['max_sequence_tokens']} tokens, chunk ceiling {ceiling}",
        ),
    ]

    try:
        scan = diagnostics.scan_payloads(_iter_index_embedding_texts(ctx, sources))
    except TokenizerAssetError as exc:
        checks.append(CheckResult("payloads", "fail", f"tokenizer unavailable: {exc}"))
        return CheckSection("Tokenizer", checks)

    if scan.scanned == 0:
        checks.append(CheckResult("payloads", "ok", "no embedding payloads indexed yet"))
    else:
        payload_status: Status = "fail" if scan.truncation_count else "ok"
        checks.append(
            CheckResult(
                "payloads",
                payload_status,
                f"{scan.scanned} scanned, max {scan.max_payload_tokens}/{scan.limit} tokens, "
                f"truncated {scan.truncation_count}",
            )
        )
    return CheckSection("Tokenizer", checks)


def _iter_index_embedding_texts(ctx: AppContext, sources: list[Any]) -> Any:
    seen_projects: set[str] = set()
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        if project_id in seen_projects:
            continue
        seen_projects.add(project_id)
        conn = ctx.project_conn(project_id)
        yield from documents_repo.iter_embedding_texts(conn)


def _ai_section(ctx: AppContext) -> CheckSection:
    """Subscription plan, Phase 4: report the configured AI provider, the
    privacy posture, and -- for a subscription provider -- whether its
    optional runtime/SDK is even detectable, plus any credential env var
    that could shadow the intended sign-in. Deliberately offline and
    secret-free: it never contacts a provider, starts a runtime, or prints
    a token. A misconfiguration here is at most a WARN (the user's own
    choice to fix), never a FAIL that would make ``doctor`` non-zero.
    """
    from ragmonk.ai import registry

    provider = ctx.config.ai.provider
    allowed = ctx.config.privacy.external_ai_allowed
    checks = [
        CheckResult("provider", "ok", f"provider={provider}, external_ai_allowed={allowed}")
    ]
    cap = registry.get_capability(provider)
    if cap is not None and cap.subscription:
        if cap.cloud_egress and not allowed:
            checks.append(
                CheckResult(
                    "privacy",
                    "warn",
                    f"{provider} is a cloud provider but privacy.external_ai_allowed=false; "
                    "set it true to use it",
                )
            )
        checks.append(_subscription_runtime_check(provider))
    return CheckSection("AI", checks)


def _subscription_runtime_check(provider: str) -> CheckResult:
    if provider == "codex":
        found = shutil.which("codex")
        if found:
            return CheckResult("runtime", "ok", "codex runtime found on PATH")
        return CheckResult(
            "runtime", "warn", "codex runtime not found on PATH; run 'ragmonk ai login codex' setup"
        )
    if provider == "github_copilot":
        import importlib.util

        from ragmonk.ai import github_copilot

        spec = importlib.util.find_spec(github_copilot._SDK_IMPORT_NAME)
        conflicting = github_copilot.has_conflicting_env()
        if spec is None:
            detail = "Copilot SDK not installed (pip install 'ragmonk[copilot]')"
            status: Status = "warn"
        elif conflicting:
            detail = f"SDK present; note env vars that may shadow sign-in: {', '.join(conflicting)}"
            status = "warn"
        else:
            detail = "Copilot SDK present"
            status = "ok"
        return CheckResult("runtime", status, detail)
    return CheckResult("runtime", "ok", f"{provider} runtime check not applicable")


def _semantic_check(ctx: AppContext, sources: list[Any]) -> CheckResult:
    from ragmonk.retrieval import embedder

    try:
        import usearch  # noqa: F401

        engine = ctx.config.search.vector.engine
        backend = "bruteforce" if engine == "bruteforce" else "usearch"
    except ImportError:
        backend = "bruteforce"

    total_vectors = 0
    total_bytes = 0
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        conn = ctx.project_conn(project_id)
        total_vectors += vector_items_repo.count_all(conn, model_id=embedder.EMBEDDING_MODEL_ID)
        index_path = paths.project_vector_index_path(project_id, ctx.home)
        if index_path.is_file():
            total_bytes += index_path.stat().st_size

    detail = f"backend {backend}, {total_vectors} vector(s), {total_bytes / (1024**2):.1f} MB index"
    return CheckResult("ann_backend", "ok", detail)


def _overall_colour(overall: str) -> str:
    if overall == "HEALTHY":
        return "green"
    if overall == "UNHEALTHY":
        return "red"
    return "yellow"


def _render_human(sections: list[CheckSection], overall: str) -> None:
    console.print("\n[bold]RagMonk Doctor[/bold]\n")
    style = {"ok": "green", "warn": "yellow", "fail": "red"}
    for section in sections:
        console.print(f"[bold]{section.name}[/bold]")
        for check in section.checks:
            colour = style[check.status]
            console.print(f"  [{colour}]{check.status.upper()}[/{colour}] {check.detail}")
        console.print()
    overall_colour = _overall_colour(overall)
    console.print(f"Result:\n  [bold {overall_colour}]{overall}[/bold {overall_colour}]")


def _sections_to_json(sections: list[CheckSection], overall: str) -> dict[str, Any]:
    return {
        "result": overall,
        "sections": [
            {
                "name": section.name,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail} for c in section.checks
                ],
            }
            for section in sections
        ],
    }


@cli_command
def doctor(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with AppContext.bootstrap() as ctx:
        sections = run_checks(ctx)
        overall = overall_status(sections)
        if json_output:
            print_json(_sections_to_json(sections, overall))
        else:
            _render_human(sections, overall)
        if overall == "UNHEALTHY":
            raise typer.Exit(code=EXIT_HEALTH_CHECK_FAILURE)


@cli_command
def health(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    with AppContext.bootstrap() as ctx:
        sections = run_checks(ctx)
        overall = overall_status(sections)
        if json_output:
            print_json({"result": overall})
        else:
            colour = _overall_colour(overall)
            console.print(f"[bold {colour}]{overall}[/bold {colour}]")
        if overall == "UNHEALTHY":
            raise typer.Exit(code=EXIT_HEALTH_CHECK_FAILURE)
