"""Detached background update check (CLI performance improvement plan,
Phase 4): a normal command that goes through ``AppContext.bootstrap()``
never itself makes a network call for this -- it only decides, from the
local cache's age, whether to spawn a short-lived, fully detached
``python -m ragpilot.update.background <home>`` process that does the one
real GitHub request and writes the result, then exits. The *next*
invocation is what reads the refreshed cache (``notifier.py``) -- see
this plan's "two-call model" diagram.

Never allowed to make the calling command slower or less reliable:
spawning is wrapped in a broad try/except (a sandbox with no fork
permission, a read-only filesystem, whatever) and the check itself
(``run_background_check``) swallows every error silently -- an offline
machine or an unreachable GitHub must never surface as an error from an
unrelated command (see this plan's Offline Behavior section). Only the
explicit ``ragpilot update check`` reports a failure.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ragpilot.core.config import UpdatesConfig
from ragpilot.update import cache
from ragpilot.update.models import UpdateCache


def is_cache_stale(cached: UpdateCache | None, *, interval_hours: int) -> bool:
    if cached is None:
        return True
    try:
        last_checked = datetime.fromisoformat(cached.last_checked)
    except ValueError:
        return True
    if last_checked.tzinfo is None:
        last_checked = last_checked.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_checked > timedelta(hours=interval_hours)


def maybe_launch_background_check(home: Path, config: UpdatesConfig) -> None:
    if not config.enabled:
        return
    cached = cache.read_cache(home)
    if not is_cache_stale(cached, interval_hours=config.check_interval_hours):
        return
    # A sandbox with no fork/exec permission, a broken interpreter path,
    # whatever -- the calling command must never fail or even notice
    # because the update subsystem couldn't spawn a helper.
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603 - fixed argv (sys.executable + literal module name), no shell
            [sys.executable, "-m", "ragpilot.update.background", str(home)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def run_background_check(home: Path) -> None:
    """The detached process's entire body -- see this module's docstring
    for why every failure here is swallowed rather than raised or
    printed. Imports ``checker``/``versioning`` locally: this module's
    own top level must stay import-light, since ``maybe_launch_background_check``
    above runs inside every normal command's ``AppContext.bootstrap()``.
    """
    from ragpilot.update import checker, versioning

    try:
        release = checker.fetch_latest_release()
    except checker.UpdateCheckError:
        return

    # Carries the previous "already notified" marker forward only when
    # the discovered latest version hasn't changed since last time -- a
    # genuinely new release must be notified once, but re-discovering the
    # same one on the next scheduled check must not re-trigger it (see
    # this plan's "do not display this message repeatedly" requirement,
    # and notifier.py).
    previous = cache.read_cache(home)
    carried_notified = (
        previous.last_notified_version
        if previous is not None and previous.latest_version == release.version
        else None
    )
    with contextlib.suppress(OSError):
        cache.write_cache(
            home,
            UpdateCache(
                last_checked=cache.now_iso(),
                installed_version=versioning.installed_version(),
                latest_version=release.version,
                release_url=release.html_url,
                last_notified_version=carried_notified,
            ),
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_background_check(Path(sys.argv[1]))
