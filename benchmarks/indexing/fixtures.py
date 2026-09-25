"""Deterministic synthetic fixture generation for the indexing
benchmarks (Phase P0). Everything here is generated content -- no
confidential or real-world data is ever committed or downloaded.

Code files are generated as small, syntactically valid Python modules
(parseable by the real Tree-sitter code processor) so a benchmark run
exercises the real code-indexing path, not just raw-file bookkeeping.
Document files reuse the repository's own tiny, already-committed
``tests/fixtures/documents`` samples (txt/md/html/csv), replicated under
unique names, so the mixed-format tier exercises real document
detection/routing without requiring PDF/DOCX-authoring libraries that
may not be installed in every environment (Docling/torch themselves are
optional runtime dependencies -- see ``runner.py``'s availability check).
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

_FIXTURES_DOCS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "documents"

# Kept small and text-based on purpose: these are the document formats
# that need no optional heavy dependency (Docling/torch) to be scanned,
# hashed and classified -- exactly the part of the pipeline P0 measures
# today. ``runner.py`` additionally copies one real ``.pdf``/``.docx``
# sample when Docling is available, for an (optional, disclosed) richer
# document-conversion tier.
_TEXT_DOC_SOURCES = ["simple.txt", "simple.md", "simple.html", "simple.csv"]

_CODE_TEMPLATE = '''"""Generated fixture module {index}."""

from __future__ import annotations


class Service{index}:
    """A small synthetic service class used only for benchmark load."""

    def __init__(self, name: str = "service_{index}") -> None:
        self.name = name
        self._counter = 0

    def handle(self, payload: dict) -> dict:
        self._counter += 1
        return {{"name": self.name, "seq": self._counter, "payload": payload}}

    def reset(self) -> None:
        self._counter = 0


def process_{index}(items: list[int]) -> int:
    total = 0
    for item in items:
        if item % 2 == 0:
            total += item
        else:
            total -= item
    return total


def helper_{index}(value: str) -> str:
    return value.strip().lower()
'''


def _write_code_file(path: Path, index: int) -> None:
    path.write_text(_CODE_TEMPLATE.format(index=index), encoding="utf-8")


def generate_code_corpus(root: Path, n_files: int, *, subdir: str = "src") -> list[Path]:
    """Writes ``n_files`` small, deterministic, syntactically valid
    Python modules under ``root/subdir``, spread across a handful of
    subdirectories (so the corpus exercises real directory-tree
    traversal, not one flat folder). Returns the written paths in a
    stable, deterministic order.
    """
    base = root / subdir
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    dirs_count = max(1, n_files // 200)
    for i in range(n_files):
        sub = base / f"pkg_{i % dirs_count:04d}"
        sub.mkdir(parents=True, exist_ok=True)
        path = sub / f"module_{i:06d}.py"
        _write_code_file(path, i)
        written.append(path)
    return written


def generate_mixed_document_corpus(root: Path, n_docs: int, *, subdir: str = "docs") -> list[Path]:
    """Replicates the repository's tiny real txt/md/html/csv document
    fixtures under unique names until ``n_docs`` files exist. Content is
    byte-identical across copies of the same source extension (still
    deterministic, still real document content -- just repeated), which
    is sufficient for measuring scan/hash/routing overhead; genuine
    per-file uniqueness is not required for a throughput benchmark.
    """
    base = root / subdir
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i in range(n_docs):
        source_name = _TEXT_DOC_SOURCES[i % len(_TEXT_DOC_SOURCES)]
        source_path = _FIXTURES_DOCS / source_name
        suffix = source_path.suffix
        dest = base / f"doc_{i:06d}{suffix}"
        shutil.copyfile(source_path, dest)
        written.append(dest)
    return written


def copy_real_binary_document_samples(root: Path, *, subdir: str = "docs_binary") -> list[Path]:
    """Copies one real ``.pdf`` and one real ``.docx`` sample from the
    repository's committed test fixtures, for the optional
    Docling-dependent benchmark tier (only meaningful when Docling/torch
    are installed -- see ``runner.py``).
    """
    base = root / subdir
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in ("sample.pdf", "document.docx"):
        source_path = _FIXTURES_DOCS / name
        if not source_path.exists():
            continue
        dest = base / name
        shutil.copyfile(source_path, dest)
        written.append(dest)
    return written


def apply_single_edit(files: list[Path], *, seed: int = 0) -> Path:
    """Appends a comment to exactly one existing file, simulating the
    single-file-edit workload.
    """
    rng = random.Random(seed)
    target = rng.choice(files)
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n# edited\n")
    return target


def apply_percent_change(files: list[Path], pct: float, *, seed: int = 0) -> list[Path]:
    """Appends a comment to ``pct`` fraction (e.g. ``0.01`` for 1%) of
    ``files``, simulating a burst of scattered edits.
    """
    rng = random.Random(seed)
    n = max(1, int(len(files) * pct))
    targets = rng.sample(files, n)
    for target in targets:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n# edited\n")
    return targets


def apply_rename(files: list[Path], *, seed: int = 0) -> tuple[Path, Path]:
    rng = random.Random(seed)
    target = rng.choice(files)
    new_path = target.with_name(f"renamed_{target.name}")
    target.rename(new_path)
    return target, new_path


def apply_delete(files: list[Path], n: int, *, seed: int = 0) -> list[Path]:
    rng = random.Random(seed)
    targets = rng.sample(files, min(n, len(files)))
    for target in targets:
        target.unlink(missing_ok=True)
    return targets
