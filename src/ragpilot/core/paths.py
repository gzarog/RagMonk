"""Resolution of RAGpilot's on-disk runtime layout.

``RAGPILOT_HOME`` always wins so tests (and power users) can redirect the
entire runtime tree without touching the real user profile.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def runtime_dir() -> Path:
    override = os.environ.get("RAGPILOT_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "RAGpilot"
        return Path.home() / "AppData" / "Local" / "RAGpilot"
    return Path.home() / ".ragpilot"


def sources_db_path(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "sources.db"


def user_config_path(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "config.yaml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".ragpilot.yaml"


def logs_dir(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "logs"


def backups_dir(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "backups"


def locks_dir(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "locks"


def tmp_dir(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "tmp"


def daemon_pid_path(home: Path | None = None) -> Path:
    """The Phase 7 daemon's PID-file -- one daemon per ``RAGPILOT_HOME``,
    mirroring ``sources.db``'s own one-per-home scope. See
    ``service/pid.py``.
    """
    return (home or runtime_dir()) / "daemon.pid"


def daemon_health_path(home: Path | None = None) -> Path:
    """Where the running daemon's heartbeat/health snapshot is written,
    so ``ragpilot daemon status``/``doctor`` can read it from a fresh CLI
    invocation without asking the daemon process itself. See
    ``service/health.py``.
    """
    return (home or runtime_dir()) / "daemon_health.json"


def install_info_path(home: Path | None = None) -> Path:
    """CLI performance improvement plan, Phase 5: install.sh/install.ps1
    write this after a successful install-script install, recording how
    and where -- the authoritative signal ``update/installer.py``'s
    ``detect_install_method`` checks before falling back to runtime
    heuristics (an editable/dev checkout, a pipx venv path, else pip).
    """
    return (home or runtime_dir()) / "install_info.json"


def update_cache_path(home: Path | None = None) -> Path:
    """CLI performance improvement plan, Phase 3: the small local cache
    ``ragpilot update check`` writes and ``ragpilot update status`` reads
    -- one per ``RAGPILOT_HOME``, mirroring ``daemon_health_path`` above.
    A later phase's startup notification reads this too, which is the
    whole point of it existing: never a synchronous GitHub call on every
    invocation. See ``update/cache.py``.
    """
    return (home or runtime_dir()) / "update.json"


def projects_dir(home: Path | None = None) -> Path:
    return (home or runtime_dir()) / "projects"


def project_id_for_path(path: Path) -> str:
    canonical = str(path.resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:12]


def project_dir(project_id: str, home: Path | None = None) -> Path:
    return projects_dir(home) / project_id


def project_db_path(project_id: str, home: Path | None = None) -> Path:
    return project_dir(project_id, home) / "knowledge.db"


def project_cache_dir(project_id: str, home: Path | None = None) -> Path:
    return project_dir(project_id, home) / "cache"


def project_vector_index_path(project_id: str, home: Path | None = None) -> Path:
    """Where a project's persistent ANN index lives (blueprint section
    3), alongside its ``knowledge.db`` -- see ``retrieval/ann.py``.
    """
    return project_dir(project_id, home) / "vectors.usearch"


def project_vector_meta_path(project_id: str, home: Path | None = None) -> Path:
    """Format/model/generation metadata for ``project_vector_index_path``
    (blueprint section 48) -- what tells a loader the on-disk index is
    stale and must be rebuilt rather than silently used.
    """
    return project_dir(project_id, home) / "vectors.meta.json"


def project_state_dir(project_id: str, home: Path | None = None) -> Path:
    return project_dir(project_id, home) / "state"


def ensure_runtime_layout(home: Path | None = None) -> Path:
    base = home or runtime_dir()
    for directory in (
        base,
        projects_dir(base),
        logs_dir(base),
        backups_dir(base),
        locks_dir(base),
        tmp_dir(base),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return base


def ensure_project_layout(project_id: str, home: Path | None = None) -> Path:
    pdir = project_dir(project_id, home)
    directories = (
        pdir,
        project_cache_dir(project_id, home),
        project_state_dir(project_id, home),
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return pdir
