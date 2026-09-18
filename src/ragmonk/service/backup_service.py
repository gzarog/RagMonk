"""Backup / restore / update operations for the admin UI.

Admin UI plan, Phase 9 (§12.1-§12.3): create and list backups, restore
(with explicit confirmation, enforced in the router), and surface update
status. Reuses ``ops.backup`` / ``ops.restore`` and the ``update`` cache
exactly as the CLI does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.ops.backup import create_backup
from ragmonk.ops.restore import restore_backup
from ragmonk.sources.registry import SourceRegistry
from ragmonk.update import cache, versioning


def list_backups(ctx: AppContext) -> list[dict[str, Any]]:
    """Backup archives found in ``<home>/backups`` (§12.1), newest first."""
    backups_dir = paths.backups_dir(ctx.home)
    entries: list[dict[str, Any]] = []
    if backups_dir.is_dir():
        for path in backups_dir.glob("*.tar.gz"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "created_at": stat.st_mtime,
                }
            )
    entries.sort(key=lambda e: e["created_at"], reverse=True)
    return entries


def create(ctx: AppContext) -> dict[str, Any]:
    """Create a backup under the same ``index`` lock the CLI takes."""
    lock = ctx.acquire_lock("index")
    try:
        sources = SourceRegistry(ctx.sources_conn, home=ctx.home).list()
        archive_path, manifest = create_backup(home=ctx.home, sources=sources)
    finally:
        lock.release()
    return {
        "archive": str(archive_path),
        "name": archive_path.name,
        "ragmonk_version": manifest.ragmonk_version,
        "created_at": manifest.created_at,
        "projects": len(manifest.projects),
    }


def restore(ctx: AppContext, archive_name: str) -> dict[str, Any]:
    """Restore a named archive from the backups directory (§12.2).

    Only archives inside ``<home>/backups`` are accepted -- the router
    passes a bare filename, never an arbitrary path, so a request can't
    point restore at a file elsewhere on disk.
    """
    backups_dir = paths.backups_dir(ctx.home)
    archive = (backups_dir / archive_name).resolve()
    if archive.parent != backups_dir.resolve() or not archive.is_file():
        raise LookupError(f"no such backup: {archive_name}")
    report = restore_backup(archive, home=ctx.home)
    return {
        "archive": report.archive,
        "sources_restored": report.sources_restored,
        "projects_restored": report.projects_restored,
        "daemon_restarted": report.daemon_restarted,
    }


def backup_path(ctx: AppContext, archive_name: str) -> Path:
    """Resolve a backup filename to a path for download, guarding against
    traversal outside the backups directory."""
    backups_dir = paths.backups_dir(ctx.home)
    archive = (backups_dir / archive_name).resolve()
    if archive.parent != backups_dir.resolve() or not archive.is_file():
        raise LookupError(f"no such backup: {archive_name}")
    return archive


def update_status(ctx: AppContext) -> dict[str, Any]:
    """Installed vs. latest-known version from the local cache (§12.3).

    Reads the local update cache only -- never contacts GitHub, matching
    ``ragmonk update status``.
    """
    installed = versioning.installed_version()
    cached = cache.read_cache(ctx.home)
    if cached is None:
        return {
            "installed_version": installed,
            "latest_version": None,
            "update_available": None,
            "channel": ctx.config.updates.channel,
            "last_checked": None,
            "release_url": None,
        }
    up_to_date = not versioning.is_newer(cached.latest_version, installed)
    return {
        "installed_version": installed,
        "latest_version": cached.latest_version,
        "update_available": not up_to_date,
        "channel": ctx.config.updates.channel,
        "last_checked": cached.last_checked,
        "release_url": cached.release_url,
    }
