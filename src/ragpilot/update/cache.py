"""Reads/writes ``<RAGPILOT_HOME>/update.json``: the small local cache
that ``ragpilot update check`` refreshes and ``ragpilot update status``
(and, in a later phase, a startup notification) reads without ever making
a network call itself.

Same write-then-rename durability pattern as ``service/health.py``'s
daemon health snapshot -- a reader on an unrelated process/invocation
must never observe a torn write.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ragpilot.core import paths
from ragpilot.update.models import UpdateCache


def write_cache(home: Path, cache: UpdateCache) -> None:
    path = paths.update_cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Suffixed with pid+tid, not a fixed name, so concurrent writers (e.g.
    # an explicit `update check` racing a later phase's background
    # checker) never consume each other's tmp file before it's renamed.
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(asdict(cache), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def read_cache(home: Path) -> UpdateCache | None:
    """Returns ``None`` for a missing, corrupt, or unrecognized cache file
    -- callers treat that as "no cached result yet" (see
    ``cli/update.py``'s ``status`` command), never as an error: this cache
    is a pure optimization, not a source of truth.
    """
    path = paths.update_cache_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return UpdateCache(
            last_checked=data["last_checked"],
            installed_version=data["installed_version"],
            latest_version=data["latest_version"],
            release_url=data["release_url"],
            last_notified_version=data.get("last_notified_version"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
