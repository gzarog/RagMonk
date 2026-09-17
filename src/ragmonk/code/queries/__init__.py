"""Loads and compiles the per-language Tree-sitter ``.scm`` query files."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import tree_sitter as ts

from ragmonk.code.parser import QUERY_FILE_BY_LANGUAGE, language_object

_QUERY_DIR = Path(__file__).parent


@cache
def load_query(language: str) -> ts.Query:
    query_name = QUERY_FILE_BY_LANGUAGE[language]
    text = (_QUERY_DIR / f"{query_name}.scm").read_text(encoding="utf-8")
    return ts.Query(language_object(language), text)
