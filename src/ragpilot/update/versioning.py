"""Version parsing/comparison for the update subsystem.

``is_valid``/``parse`` are deliberately strict -- plain
``MAJOR.MINOR.PATCH`` only, no SemVer 2.0 pre-release/build-metadata --
because they validate an *untrusted*, externally-sourced version (a
GitHub release tag; see ``checker.py``'s Security Requirements): this
project's own release process (this plan's Phase 6) only ever produces
tags in that exact shape, so anything else is rejected outright rather
than half-handled.

``is_newer``'s *installed* side is not held to that same strict shape,
though: an editable/unreleased build's version -- derived from git tags
via hatch-vcs, see ``pyproject.toml``'s ``[tool.hatch.version]`` -- is a
PEP 440 *dev* version (e.g. ``"0.1.dev39+gd6cd42e"``) rather than a plain
release. ``packaging.version`` (already a transitive dependency of the
packaging toolchain itself) parses and orders that correctly -- a dev
build between two releases sorts as older than the next one, exactly as
PEP 440 intends -- so comparing against it never mistakes "no tag yet"
for "no update available".
"""

from __future__ import annotations

import re

from packaging.version import InvalidVersion, Version

from ragpilot import __version__

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def installed_version() -> str:
    return __version__


def normalize(version: str) -> str:
    """Strips a leading ``v``/``V`` -- git tags are ``v0.1.8``, but this
    codebase's own ``__version__`` and every comparison here work on the
    bare form. Callers should normalize any externally-sourced version
    (e.g. a GitHub release's ``tag_name``) before comparing or storing it.
    """
    return version[1:] if version[:1] in ("v", "V") else version


def is_valid(version: str) -> bool:
    return _SEMVER_RE.match(normalize(version)) is not None


def parse(version: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.match(normalize(version))
    if match is None:
        raise ValueError(f"{version!r} is not a valid MAJOR.MINOR.PATCH version")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def is_newer(candidate: str, than: str) -> bool:
    """True when ``candidate`` -- always an untrusted release tag, held to
    ``is_valid``'s strict shape -- is a newer version than ``than``,
    normally the installed version (see this module's docstring for why
    that side is parsed with the more permissive PEP 440 rules instead).
    An invalid ``candidate`` is never considered newer rather than
    raising, so a malformed or unexpected tag degrades to "no update
    available" instead of crashing the check (this plan's Security
    Requirements); same for a ``than`` that even PEP 440 can't parse.
    """
    if not is_valid(candidate):
        return False
    try:
        return Version(normalize(candidate)) > Version(normalize(than))
    except InvalidVersion:
        return False
