"""Installing a newer RAGpilot release (CLI performance improvement plan,
Phase 5): installation-method detection, the per-method upgrade command,
and the "upgrade -> restart into new code -> migrate -> health check"
sequence ``ragpilot update install`` (``cli/update.py``) runs.

Security (this plan's Security Requirements section): every upgrade
targets an immutable release *tag* -- ``checker.py`` already validated
it's a real semantic version of ``gzarog/Ragpilotv2``'s own releases --
never ``main`` and never a URL taken from the release description; the
commands built here only ever reference this project's own hardcoded
GitHub URLs plus that validated tag.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ragpilot.core import paths

_DISTRIBUTION_NAME = "ragpilot"

_INSTALL_SH_URL = "https://raw.githubusercontent.com/gzarog/Ragpilotv2/main/install.sh"
_INSTALL_PS1_URL = "https://raw.githubusercontent.com/gzarog/Ragpilotv2/main/install.ps1"
_GIT_URL = "https://github.com/gzarog/Ragpilotv2.git"


class InstallMethod(StrEnum):
    INSTALL_SCRIPT = "install-script"
    PIP = "pip"
    PIPX = "pipx"
    EDITABLE = "editable"
    UNKNOWN = "unknown"


class UpdateInstallError(Exception):
    """This RAGpilot install can't be upgraded automatically (an
    editable/dev checkout, an undetectable install method), or an upgrade
    step itself failed. ``cli/update.py`` maps this to a clear,
    non-traceback CLI error.
    """


@dataclass(frozen=True)
class InstallMetadata:
    """``<RAGPILOT_HOME>/install_info.json``'s schema -- written by
    install.sh/install.ps1 on a successful install-script install.
    """

    install_method: InstallMethod
    repository: str
    install_dir: str | None = None
    venv_dir: str | None = None
    bin_dir: str | None = None


@dataclass(frozen=True)
class InstallOutcome:
    installed_version: str
    upgraded: bool
    migrations_applied: bool
    healthy: bool


# ``(argv, extra_env) -> exit code``. Real callers leave this unset (see
# ``_default_runner``); tests inject a fake that never spawns a real
# process -- the same DI seam pattern as ``ai/factory.py``'s providers.
CommandRunner = Callable[[Sequence[str], "dict[str, str] | None"], int]


def _default_runner(cmd: Sequence[str], env: dict[str, str] | None = None) -> int:
    merged_env = {**os.environ, **env} if env else None
    return subprocess.run(list(cmd), env=merged_env, check=False).returncode  # noqa: S603 - argv is built entirely from hardcoded URLs/paths plus a checker.py-validated semver tag, never from the release description or other untrusted input


def read_install_metadata(home: Path) -> InstallMetadata | None:
    path = paths.install_info_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return InstallMetadata(
            install_method=InstallMethod(data["install_method"]),
            repository=data["repository"],
            install_dir=data.get("install_dir"),
            venv_dir=data.get("venv_dir"),
            bin_dir=data.get("bin_dir"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _is_editable_install() -> bool:
    try:
        dist = importlib.metadata.distribution(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return False
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    return bool(data.get("dir_info", {}).get("editable"))


def _is_pipx_install() -> bool:
    # pipx's own default venv layout (~/.local/pipx/venvs/<pkg> on POSIX,
    # %LOCALAPPDATA%\pipx\venvs\<pkg> on Windows) -- a best-effort guess
    # (not a documented pipx guarantee), used only as a fallback when
    # install_info.json doesn't exist.
    return "pipx" in Path(sys.prefix).parts


def detect_install_method(home: Path) -> InstallMethod:
    """``install_info.json`` (written by install.sh/install.ps1) is
    authoritative when present; otherwise a runtime heuristic: editable/
    dev checkout (PEP 610 ``direct_url.json``), then a pipx-shaped venv
    path, then "pip" as the default for any other installed distribution,
    else "unknown" (no ``ragpilot`` distribution metadata at all -- e.g.
    running straight from a source checkout with no install step).
    """
    stored = read_install_metadata(home)
    if stored is not None:
        return stored.install_method
    if _is_editable_install():
        return InstallMethod.EDITABLE
    if _is_pipx_install():
        return InstallMethod.PIPX
    try:
        importlib.metadata.distribution(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return InstallMethod.UNKNOWN
    return InstallMethod.PIP


def _upgrade_invocation(method: InstallMethod, tag: str) -> tuple[list[str], dict[str, str] | None]:
    """Returns ``(argv, extra_env)`` for upgrading to git tag ``tag``.
    ``tag`` only ever reaches a subprocess via ``extra_env`` (the
    install-script path) or as one ``git+URL@tag`` argv element (pip/
    pipx, never shell-interpreted) -- never interpolated into a shell
    string, even though ``checker.py`` already restricts it to
    ``v?\\d+\\.\\d+\\.\\d+`` before it ever reaches here.
    """
    if method is InstallMethod.INSTALL_SCRIPT:
        if sys.platform == "win32":
            return (
                ["powershell", "-NoProfile", "-Command", f"irm {_INSTALL_PS1_URL} | iex"],
                {"RAGPILOT_REF": tag},
            )
        return (["sh", "-c", f"curl -fsSL {_INSTALL_SH_URL} | sh"], {"RAGPILOT_REF": tag})
    if method is InstallMethod.PIP:
        return (
            [sys.executable, "-m", "pip", "install", "--upgrade", f"git+{_GIT_URL}@{tag}"],
            None,
        )
    if method is InstallMethod.PIPX:
        return (["pipx", "install", "--force", f"git+{_GIT_URL}@{tag}"], None)
    raise UpdateInstallError(f"no upgrade command for install method {method.value!r}")


def _manual_instructions(method: InstallMethod) -> str:
    if method is InstallMethod.EDITABLE:
        return (
            "this is an editable/dev RAGpilot install (`pip install -e .`); "
            "upgrade it with `git pull` in the source checkout instead."
        )
    return (
        "could not determine how this RAGpilot was installed. Upgrade manually:\n\n"
        f"    curl -fsSL {_INSTALL_SH_URL} | sh\n\n"
        f"(Windows: irm {_INSTALL_PS1_URL} | iex)\n\n"
        f'or, for a pip install: python -m pip install --upgrade "git+{_GIT_URL}@<tag>"'
    )


def install_latest(home: Path, *, runner: CommandRunner | None = None) -> InstallOutcome:
    """The execution sequence this plan's Phase 5 describes: check latest
    release, validate it's actually newer, determine the install method,
    upgrade, then -- via ``sys.executable`` again, so this picks up the
    just-upgraded code rather than this (old) process's already-imported
    modules -- run schema migrations (``ragpilot upgrade``) and a health
    check (``ragpilot doctor``), reusing those existing commands rather
    than duplicating their logic.
    """
    from ragpilot.update import checker, versioning

    run = runner or _default_runner

    try:
        release = checker.fetch_latest_release()
    except checker.UpdateCheckError as exc:
        raise UpdateInstallError(f"could not check for updates: {exc}") from exc

    installed = versioning.installed_version()
    if not versioning.is_newer(release.version, installed):
        return InstallOutcome(
            installed_version=installed, upgraded=False, migrations_applied=False, healthy=True
        )

    method = detect_install_method(home)
    if method in (InstallMethod.EDITABLE, InstallMethod.UNKNOWN):
        raise UpdateInstallError(_manual_instructions(method))

    upgrade_argv, upgrade_env = _upgrade_invocation(method, release.tag_name)
    exit_code = run(upgrade_argv, upgrade_env)
    if exit_code != 0:
        raise UpdateInstallError(
            f"upgrade command exited with code {exit_code}: {' '.join(upgrade_argv)}"
        )

    migrations_applied = run([sys.executable, "-m", "ragpilot.cli.main", "upgrade"], None) == 0
    healthy = run([sys.executable, "-m", "ragpilot.cli.main", "doctor"], None) == 0

    return InstallOutcome(
        installed_version=release.version,
        upgraded=True,
        migrations_applied=migrations_applied,
        healthy=healthy,
    )
