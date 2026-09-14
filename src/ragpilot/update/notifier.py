"""Startup update notification (CLI performance improvement plan, Phase
4): reads the local ``update.json`` cache only -- never GitHub -- and
prints at most one notification per newly-discovered version. Always to
stderr, never stdout: a command's stdout may be a `--json` payload, or
(``ragpilot serve --mcp``) the MCP stdio transport's JSON-RPC channel
itself, and a notification banner must never land inside either.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path

from rich.console import Console

from ragpilot.core.config import UpdatesConfig
from ragpilot.update import cache, versioning


def maybe_notify(home: Path, config: UpdatesConfig, *, console: Console | None = None) -> None:
    if not (config.enabled and config.notify):
        return
    cached = cache.read_cache(home)
    if cached is None:
        return
    installed = versioning.installed_version()
    if not versioning.is_newer(cached.latest_version, installed):
        return
    if cached.last_notified_version == cached.latest_version:
        # Already shown for this exact version -- background.py resets
        # this marker only when a genuinely different version is found.
        return

    out = console or Console(stderr=True)
    out.print(
        f"\n[bold]A newer RAGpilot version is available:[/bold] "
        f"{installed} → {cached.latest_version}"
    )
    out.print("Run `ragpilot update install` to upgrade.")

    # Worst case (write fails) the same notification shows again next
    # time -- never worth failing (or even erroring loudly in) the
    # calling command.
    with contextlib.suppress(OSError):
        cache.write_cache(home, replace(cached, last_notified_version=cached.latest_version))
