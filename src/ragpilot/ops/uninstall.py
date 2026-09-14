"""``ragpilot uninstall``: removes the installed RAGpilot application
and, by default, all of its data too.

Two independent steps, always in this order:

1. **Purge data** (default; skipped with ``--keep-data``, see
   ``cli/uninstall.py``): stop a running daemon first -- the same
   pre-delete safety step ``ops/restore.py`` takes before swapping in a
   backup, via the same shared ``service/pid.py`` helper -- then delete
   ``RAGPILOT_HOME`` entirely (databases, config, backups, logs,
   ``update.json``, ``install_info.json``).
2. **Remove the application itself**, dispatched by installation method
   (``update/installer.py``'s ``detect_install_method`` -- reused rather
   than re-implemented, since it is exactly the same "how was this
   installed" question ``ragpilot update install`` already answers). An
   editable/dev install or an undetectable method is never
   auto-removed; ``plan_uninstall`` reports manual instructions instead.

Deliberately narrow about what gets deleted for an install-script
install: only ``venv_dir`` and ``install_dir/app`` (install.sh/
install.ps1's own layout) and the single launcher file in ``bin_dir`` --
never ``install_dir`` as a whole, since it can (and by default does)
share a parent directory with ``RAGPILOT_HOME``, and never anything else
that happens to live in the shared ``bin_dir``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ragpilot.core.errors import RagpilotError
from ragpilot.service import pid
from ragpilot.update.installer import (
    CommandRunner,
    InstallMethod,
    detect_install_method,
    read_install_metadata,
)


@dataclass(frozen=True)
class UninstallPlan:
    method: InstallMethod
    can_auto_remove_app: bool
    app_paths: list[Path] = field(default_factory=list)
    manual_instructions: str | None = None


def _launcher_name() -> str:
    return "ragpilot.cmd" if sys.platform == "win32" else "ragpilot"


def _manual_instructions(method: InstallMethod) -> str:
    if method is InstallMethod.EDITABLE:
        return (
            "this is an editable/dev RAGpilot install (`pip install -e .`); remove it by "
            "deleting your source checkout (and optionally `pip uninstall ragpilot`)."
        )
    return (
        "could not determine how RAGpilot was installed; remove it manually, e.g. "
        "`pip uninstall ragpilot` or `pipx uninstall ragpilot`, or delete its install "
        "directory."
    )


def plan_uninstall(home: Path) -> UninstallPlan:
    method = detect_install_method(home)

    if method is InstallMethod.INSTALL_SCRIPT:
        metadata = read_install_metadata(home)
        if metadata is not None and metadata.venv_dir and metadata.install_dir and metadata.bin_dir:
            app_paths = [
                Path(metadata.venv_dir),
                Path(metadata.install_dir) / "app",
                Path(metadata.bin_dir) / _launcher_name(),
            ]
            return UninstallPlan(method=method, can_auto_remove_app=True, app_paths=app_paths)
        # install_info.json said "install-script" but is missing the
        # paths needed to safely remove anything -- never guess at
        # install/venv/bin directories, since a wrong guess could delete
        # something this install didn't create.
        return UninstallPlan(
            method=method,
            can_auto_remove_app=False,
            manual_instructions=_manual_instructions(method),
        )

    if method in (InstallMethod.PIP, InstallMethod.PIPX):
        return UninstallPlan(method=method, can_auto_remove_app=True)

    return UninstallPlan(
        method=method, can_auto_remove_app=False, manual_instructions=_manual_instructions(method)
    )


def _default_runner(cmd: list[str], env: dict[str, str] | None = None) -> int:
    merged_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, env=merged_env, check=False).returncode  # noqa: S603 - fixed argv, no shell, no untrusted input


def _remove_install_script_files(app_paths: list[Path]) -> bool:
    venv_dir, app_dir, launcher = app_paths
    if sys.platform == "win32":
        return _detached_delete_windows([venv_dir, app_dir], launcher)
    # POSIX: unlinking files a running process has open (this interpreter,
    # executing from inside venv_dir) is safe -- the kernel keeps the
    # inode alive until the process exits, so a direct, synchronous
    # rmtree works reliably, unlike Windows' file-locking model.
    for directory in (venv_dir, app_dir):
        shutil.rmtree(directory, ignore_errors=True)
    launcher.unlink(missing_ok=True)
    return True


def _detached_delete_windows(  # pragma: no cover - exercised only on Windows
    directories: list[Path], launcher: Path
) -> bool:
    """Deleting files this running process has open can fail outright on
    Windows (locked DLL/executable handles) -- a short-lived, fully
    detached process, started with no console/parent link, waits for
    *this* process to exit first instead of trying to delete alongside it.
    """
    dir_list = ", ".join(f"'{d}'" for d in directories)
    script = (
        f"Start-Sleep -Seconds 2; "
        f"Remove-Item -Recurse -Force {dir_list} -ErrorAction SilentlyContinue; "
        f"Remove-Item -Force '{launcher}' -ErrorAction SilentlyContinue"
    )
    # getattr, not a direct attribute access: these two flags only exist
    # in the `subprocess` module on Windows (typeshed gates them the same
    # way), and this function -- though only ever called on Windows -- is
    # still defined and type-checked on every platform.
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        subprocess.Popen(  # noqa: S603 - fixed, locally-constructed script, no untrusted input
            ["powershell", "-NoProfile", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError:
        return False
    return True


def remove_application(plan: UninstallPlan, *, runner: CommandRunner | None = None) -> bool:
    if not plan.can_auto_remove_app:
        return False

    run = runner or _default_runner

    if plan.method is InstallMethod.PIP:
        return run([sys.executable, "-m", "pip", "uninstall", "-y", "ragpilot"], None) == 0
    if plan.method is InstallMethod.PIPX:
        return run(["pipx", "uninstall", "ragpilot"], None) == 0
    if plan.method is InstallMethod.INSTALL_SCRIPT:
        return _remove_install_script_files(plan.app_paths)
    return False


def purge_home(home: Path) -> bool:
    """Stops a running daemon, then deletes ``home`` entirely. Returns
    whether there was anything to delete. Raises if a running daemon
    won't stop -- deleting its files out from under it is never attempted.
    """
    if not home.exists():
        return False
    pid.stop_and_wait(home, action="delete this data", error_cls=RagpilotError)
    shutil.rmtree(home, ignore_errors=True)
    return True
