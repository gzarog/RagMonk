"""Coarse file classification by extension.

Real language/format-aware parsing arrives in later phases; Phase 1 only
needs enough of a signal to route files into the processor registry.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.core.models import FileKind

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".c", ".h",
        ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".swift", ".m",
        ".sh", ".bash", ".zsh", ".ps1", ".sql", ".lua", ".pl", ".r",
    }
)

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
        ".md", ".markdown", ".txt", ".rst", ".csv", ".html", ".htm",
        ".odt", ".rtf",
        # Search Quality Improvement Plan, Phase 10: genuinely converted
        # (see documents/docling_adapter.py's EXTENSION_TO_FORMAT) --
        # .ods/.odp join .odt, .epub is new, and the image extensions are
        # detected unconditionally even though real extraction from them
        # additionally requires documents.image_ocr to be enabled (see
        # that module's docstring) -- detection and "will actually be
        # converted" are already two different questions for every format
        # in this set, same as .doc/.ppt/.xls/.rtf above.
        ".ods", ".odp", ".epub",
        ".png", ".jpg", ".jpeg", ".tif", ".tiff",
    }
)


def classify(path: Path) -> FileKind:
    suffix = path.suffix.lower()
    if suffix in CODE_EXTENSIONS:
        return FileKind.CODE
    if suffix in DOCUMENT_EXTENSIONS:
        return FileKind.DOCUMENT
    return FileKind.UNKNOWN
