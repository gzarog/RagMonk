"""Synthetic source-code corpus generation for the real-server indexing
benchmark (``benchmarks/server_indexing``).

Unlike ``benchmarks/indexing/fixtures.py`` (which generates isolated,
self-contained modules to keep the offline SQLite suite's scan/hash cost
representative but simple), this corpus deliberately wires files
together: every generated module imports and calls functions defined in
one or two *other* generated modules, in a fixed, deterministic chain.
That is what gives a real indexing pass something to do for symbol
resolution, cross-file call-graph linking (``callers``/``callees``) and
reference indexing -- the whole point of this benchmark is exercising a
real server backend's entity/relationship publish path, not just file
bytes.

Everything here is generated, synthetic content. No confidential or
real-world data is ever generated, downloaded, or committed.
"""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass
from pathlib import Path

_PY_TEMPLATE = '''"""Generated module {index}."""

from __future__ import annotations

from pkg.mod_{prev_index} import helper_{prev_index}


class Widget{index}:
    """A small synthetic class used only for benchmark load."""

    def __init__(self, name: str = "widget_{index}") -> None:
        self.name = name
        self._count = 0

    def handle(self, payload: dict) -> dict:
        self._count += 1
        enriched = helper_{prev_index}(payload)
        return {{"name": self.name, "seq": self._count, "payload": enriched}}


def process_{index}(items: list[int]) -> int:
    total = 0
    for item in items:
        if item % 2 == 0:
            total += item
        else:
            total -= item
    return total + helper_{prev_index}({{"count": len(items)}}).get("count", 0)


def helper_{index}(value: dict) -> dict:
    value = dict(value)
    value["touched_by"] = "module_{index}"
    return value


def orchestrate_{index}(items: list[int]) -> int:
    """Calls into the previous module to build a real cross-file call
    chain across the whole corpus (module N calls module N-1, which
    calls module N-2, ...).
    """
    return process_{index}(items) + process_{prev_index}(items)
'''

_JS_TEMPLATE = """// Generated module {index}
const {{ helper{prev_index} }} = require("./mod_{prev_index}");

function process{index}(items) {{
    let total = 0;
    for (const item of items) {{
        total += item % 2 === 0 ? item : -item;
    }}
    return total + helper{prev_index}({{ count: items.length }}).count;
}}

function helper{index}(value) {{
    return Object.assign({{}}, value, {{ touchedBy: "module_{index}" }});
}}

module.exports = {{ process{index}, helper{index} }};
"""


@dataclass(frozen=True)
class CorpusFile:
    path: Path
    language: str


def _write_py(root: Path, index: int) -> Path:
    prev_index = (index - 1) % 1_000_000 if index > 0 else 0
    path = root / "pkg" / f"mod_{index}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if index == 0:
        # Module 0 has no predecessor: give it a self-contained body so
        # the import chain above is always satisfiable.
        path.write_text(
            '"""Generated module 0 (chain root)."""\n\n'
            "from __future__ import annotations\n\n\n"
            "class Widget0:\n"
            '    def __init__(self, name: str = "widget_0") -> None:\n'
            "        self.name = name\n"
            "        self._count = 0\n\n"
            "    def handle(self, payload: dict) -> dict:\n"
            "        self._count += 1\n"
            "        return {\"name\": self.name, \"seq\": self._count, \"payload\": payload}\n\n\n"
            "def process_0(items: list[int]) -> int:\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item if item % 2 == 0 else -item\n"
            "    return total\n\n\n"
            'def helper_0(value: dict) -> dict:\n'
            "    value = dict(value)\n"
            '    value["touched_by"] = "module_0"\n'
            "    return value\n\n\n"
            "def orchestrate_0(items: list[int]) -> int:\n"
            "    return process_0(items)\n"
        )
    else:
        path.write_text(_PY_TEMPLATE.format(index=index, prev_index=prev_index))
    return path


def _write_js(root: Path, index: int) -> Path:
    prev_index = (index - 1) % 1_000_000 if index > 0 else 0
    path = root / "web" / f"mod_{index}.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    if index == 0:
        path.write_text(
            "// Generated module 0 (chain root)\n"
            "function process0(items) {\n"
            "    let total = 0;\n"
            "    for (const item of items) { total += item % 2 === 0 ? item : -item; }\n"
            "    return total;\n"
            "}\n\n"
            "function helper0(value) {\n"
            '    return Object.assign({}, value, { touchedBy: "module_0" });\n'
            "}\n\n"
            "module.exports = { process0, helper0 };\n"
        )
    else:
        path.write_text(_JS_TEMPLATE.format(index=index, prev_index=prev_index))
    return path


_README_TEMPLATE = """# Generated package {index}

Synthetic documentation for benchmark module {index}. It documents
`process_{index}` and `helper_{index}` from `pkg/mod_{index}.py`, which
are exercised by the server-indexing benchmark's lexical queries.
"""


def generate_corpus(
    root: Path,
    n_files: int,
    *,
    js_ratio: float = 0.2,
    docs_ratio: float = 0.05,
    seed: int = 1234,
) -> list[CorpusFile]:
    """Generates ``n_files`` source files (plus a small fraction of
    Markdown docs) under ``root``, wired together with cross-file
    imports/calls in a deterministic chain. Returns every generated file
    with its language tag. Deterministic for a given ``(root, n_files,
    seed)`` -- re-running with the same arguments reproduces the same
    corpus, which is what lets the incremental-benchmark variant compute
    "files changed" against a known baseline.
    """
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    n_js = int(n_files * js_ratio)
    n_py = n_files - n_js
    files: list[CorpusFile] = []
    for i in range(n_py):
        files.append(CorpusFile(path=_write_py(root, i), language="python"))
    for i in range(n_js):
        files.append(CorpusFile(path=_write_js(root, i), language="javascript"))

    n_docs = max(1, int(n_files * docs_ratio))
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_indices = rng.sample(range(n_py), k=min(n_docs, n_py)) if n_py else []
    for i in doc_indices:
        doc_path = docs_dir / f"module_{i}.md"
        doc_path.write_text(_README_TEMPLATE.format(index=i))
        files.append(CorpusFile(path=doc_path, language="markdown"))

    return files


def apply_incremental_changes(
    root: Path,
    files: list[CorpusFile],
    *,
    modify_pct: float = 0.02,
    add_pct: float = 0.01,
    delete_pct: float = 0.01,
    rename_pct: float = 0.01,
    seed: int = 5678,
) -> dict[str, list[str]]:
    """Mutates a previously generated corpus in place: modifies
    ``modify_pct`` of files (appends a new function -- a real content
    change, not a touch), adds ``add_pct`` new files (fresh module
    indices continuing the chain), deletes ``delete_pct``, and renames
    ``rename_pct``. Returns the relative paths affected under each
    action, so the caller can independently verify the incremental index
    pass only reprocessed what actually changed.
    """
    rng = random.Random(seed)
    py_files = [f for f in files if f.language == "python" and f.path.exists()]
    rng.shuffle(py_files)

    n = len(py_files)
    n_modify = max(1, int(n * modify_pct))
    n_add = max(1, int(n * add_pct))
    n_delete = max(1, int(n * delete_pct))
    n_rename = max(1, int(n * rename_pct))

    cursor = 0
    modified = py_files[cursor : cursor + n_modify]
    cursor += n_modify
    to_delete = py_files[cursor : cursor + n_delete]
    cursor += n_delete
    to_rename = py_files[cursor : cursor + n_rename]
    cursor += n_rename

    changed: dict[str, list[str]] = {"modified": [], "added": [], "deleted": [], "renamed": []}

    for f in modified:
        with f.path.open("a") as fh:
            fh.write(
                f"\n\ndef extra_benchmark_edit_{rng.randint(0, 1_000_000)}() -> int:\n"
                "    return 42\n"
            )
        changed["modified"].append(str(f.path.relative_to(root)))

    for f in to_delete:
        f.path.unlink(missing_ok=True)
        changed["deleted"].append(str(f.path.relative_to(root)))

    for f in to_rename:
        if not f.path.exists():
            continue
        new_path = f.path.with_name(f.path.stem + "_renamed" + f.path.suffix)
        shutil.move(str(f.path), str(new_path))
        changed["renamed"].append(str(new_path.relative_to(root)))

    base_index = 10_000_000
    for i in range(n_add):
        new_index = base_index + i
        new_path = root / "pkg" / f"mod_{new_index}.py"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(
            f'"""Generated added module {new_index}."""\n\n'
            "from __future__ import annotations\n\n\n"
            f"def process_{new_index}(items: list[int]) -> int:\n"
            "    return sum(items)\n"
        )
        changed["added"].append(str(new_path.relative_to(root)))

    return changed


__all__ = ["CorpusFile", "generate_corpus", "apply_incremental_changes"]
