"""Filename patterns for secret-like files that must never be indexed."""

from __future__ import annotations

import fnmatch

SECRET_FILENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "credentials*",
    "secrets*",
)


def is_secret_filename(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_FILENAME_PATTERNS)
