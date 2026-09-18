"""Log viewer for the admin UI.

Admin UI plan, Phase 10 (§13.1): read ``<home>/logs/ragmonk.log`` (the
line-delimited JSON the ``JsonFormatter`` writes) newest-first, with
level / component / text filters. Reading only -- the UI never writes to
the log.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def read_logs(
    ctx: AppContext,
    *,
    level: str | None = None,
    component: str | None = None,
    query: str | None = None,
    errors_only: bool = False,
    limit: int = 300,
) -> dict[str, Any]:
    log_path = paths.logs_dir(ctx.home) / "ragmonk.log"
    entries: list[dict[str, Any]] = []
    components: set[str] = set()

    if log_path.is_file():
        # Tail the file: keep only the last ``limit * 4`` raw lines in
        # memory (before filtering) so a huge log never loads wholesale.
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = deque(handle, maxlen=max(limit * 4, limit))
        except OSError:
            tail = deque()

        for raw in reversed(tail):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                record = {"level": "INFO", "component": "-", "event": raw, "timestamp": None}
            components.add(str(record.get("component", "-")))

            record_level = str(record.get("level", "INFO")).upper()
            if errors_only and record_level not in ("ERROR", "CRITICAL"):
                continue
            if level and record_level != level.upper():
                continue
            if component and str(record.get("component")) != component:
                continue
            if query and query.lower() not in raw.lower():
                continue
            entries.append(record)
            if len(entries) >= limit:
                break

    return {
        "entries": entries,
        "levels": list(_LEVELS),
        "components": sorted(components),
        "log_path": str(log_path),
    }
