#!/usr/bin/env python3
"""Branding audit gate: fail if any old "Ragpilot" product identifier
survives outside a narrow, deliberate allowlist.

RagMonk is a clean rename of the former Ragpilot/RAGpilot application (see
CHANGELOG.md and README.md). Every active surface -- application code,
packaging, current documentation, tests, fixtures, workflows, installers,
comments, and generated metadata -- must use only the new ``ragmonk`` /
``RagMonk`` / ``RAGMONK_`` identifiers.

Old identifiers are allowed only in three places:

1. This audit script's own pattern definitions (they must name what they
   forbid).
2. Factual historical changelog entries (``CHANGELOG.md``) -- immutable
   record of what shipped under the old name.
3. The single clean-break / cleanup note that explains old installations
   are unsupported -- wrapped in ``branding-audit-allow`` markers so it is
   scoped explicitly rather than blanket-excused.

Usage:
    python scripts/check_branding.py

Exits non-zero (listing every offending file:line) if any disallowed old
identifier is found. Run in CI (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# One case-insensitive pattern catches every old-name variant: "ragpilot",
# "RAGpilot", "Ragpilot", "RAGPILOT" (and "RAGPILOT_"), and "Ragpilotv2"
# (which contains "ragpilot"). Any occurrence is a finding unless the file
# or the surrounding block is allowlisted below.
FORBIDDEN = re.compile(r"ragpilot", re.IGNORECASE)

# Whole files exempt from the audit.
ALLOWLISTED_FILES = frozenset(
    {
        # This script's own pattern definitions (this file).
        "scripts/check_branding.py",
        # Factual historical record of what shipped under the old name.
        "CHANGELOG.md",
    }
)

# Lines between these markers are exempt (the single clean-break/cleanup
# note). The markers themselves are HTML comments, invisible in rendered
# Markdown.
ALLOW_START = "branding-audit-allow-start"
ALLOW_END = "branding-audit-allow-end"


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return out


def _scan_file(rel: str) -> list[tuple[int, str]]:
    path = REPO_ROOT / rel
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in data:  # binary
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []

    findings: list[tuple[int, str]] = []
    in_allow_block = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_START in line:
            in_allow_block = True
            continue
        if ALLOW_END in line:
            in_allow_block = False
            continue
        if in_allow_block:
            continue
        if FORBIDDEN.search(line):
            findings.append((lineno, line.strip()))
    return findings


def main() -> int:
    total = 0
    for rel in _tracked_text_files():
        if rel in ALLOWLISTED_FILES:
            continue
        for lineno, line in _scan_file(rel):
            print(f"{rel}:{lineno}: {line}")
            total += 1

    if total:
        print(
            f"\nBranding audit FAILED: {total} disallowed old identifier(s) found.\n"
            "Replace them with the RagMonk equivalents, or (only for the "
            "clean-break note) wrap the section in "
            f"<!-- {ALLOW_START} --> / <!-- {ALLOW_END} --> markers.",
            file=sys.stderr,
        )
        return 1

    print("Branding audit passed: no disallowed old identifiers found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
