"""Language detection and Tree-sitter parsing.

Uses the ``tree-sitter-language-pack`` PyPI distribution for prebuilt
grammar bindings rather than compiling grammars from source: it ships
native wheels bundling every grammar Phase 2 targets (see
``SUPPORTED_LANGUAGES``), so ``pip install`` is the entire toolchain
requirement on every CI platform (Linux/macOS/Windows). ``tree_sitter_languages``
(the older, now-unmaintained package) was considered but is not compatible
with tree-sitter>=0.24 wheels; the language-pack fork replaced it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import tree_sitter as ts
from tree_sitter_language_pack import get_language, get_parser

# Extension -> language-pack language name. ``tsx`` gets its own grammar
# (JSX syntax extends TypeScript's) but reuses typescript.scm: the capture
# patterns it needs (classes/functions/imports/calls) share node type names
# with plain TypeScript.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
}

# Query file each language name loads from code/queries/<name>.scm. ``tsx``
# has no query file of its own -- it shares typescript's.
QUERY_FILE_BY_LANGUAGE: dict[str, str] = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "tsx": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "csharp": "csharp",
}

SUPPORTED_LANGUAGES: frozenset[str] = frozenset(LANGUAGE_BY_EXTENSION.values())


def detect_language(path: Path) -> str | None:
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower())


@cache
def _parser(language: str) -> ts.Parser:
    return get_parser(language)


@cache
def language_object(language: str) -> ts.Language:
    return get_language(language)


def parse(source: bytes, language: str) -> ts.Tree:
    return _parser(language).parse(source)
