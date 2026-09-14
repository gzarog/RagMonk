"""Data types shared across the update subsystem."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReleaseInfo:
    """One GitHub release, trimmed to exactly what the update subsystem
    needs from the releases API response.
    """

    version: str  # normalized: no leading "v" (see versioning.normalize)
    tag_name: str  # the real git tag, e.g. "v0.1.8" -- what an install pins to
    html_url: str


@dataclass(frozen=True)
class UpdateCache:
    """``<RAGPILOT_HOME>/update.json``'s schema -- see ``update/cache.py``.

    ``last_notified_version`` exists so a later phase's startup
    notification can show a newer version once rather than on every
    invocation; unused (always ``None``) until that phase.
    """

    last_checked: str  # ISO 8601 UTC
    installed_version: str
    latest_version: str
    release_url: str
    last_notified_version: str | None = None
