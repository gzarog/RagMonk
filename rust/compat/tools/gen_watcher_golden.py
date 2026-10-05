"""Generate RUST-11 watcher golden fixtures from the Python reference.

    python rust/compat/tools/gen_watcher_golden.py

* ``targeted``: builds TREE, indexes it fully, then runs STEPS. Each
  step edits the tree and runs one targeted pass
  (``ScanRequest(full=False, changed_paths=...)``, what a watcher
  trigger produces) through ``run_source_pass``. For each step the
  generator records the pass counters and the files left in the index
  (relative path, status).
* ``network_diff``: ``watcher.network.diff`` over fixed fingerprints.

Writes rust/compat/golden/watcher.json.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "golden" / "watcher.json"

TREE = {
    ".gitignore": "*.log\n",
    "src/app.py": "def main():\n    return 1\n",
    "src/util/helpers.ts": "export const x = 1;\n",
    "docs/guide.md": "# Guide\n\nUse it.\n",
    "lib/a.py": "A = 1\n",
    "lib/b.py": "B = 2\n",
}

# write/delete/move edit the tree, then `targets` (relative to the root,
# or "@outside" for a path outside it) form the targeted pass.
STEPS = [
    {
        "name": "edit_add_ignored_dir",
        "write": {
            "src/app.py": "def main():\n    return 2\n",
            "src/new.py": "N = 3\n",
            "src/x.log": "noise\n",
        },
        "targets": ["src/app.py", "src/new.py", "src/x.log", "src"],
    },
    {
        "name": "rename_same_batch",
        "move": [["docs/guide.md", "docs/guide2.md"]],
        "targets": ["docs/guide.md", "docs/guide2.md"],
    },
    {
        "name": "delete_and_unknown",
        "delete": ["src/util/helpers.ts"],
        "targets": ["src/util/helpers.ts", "src/never_existed.py"],
    },
    {
        "name": "rename_split_new_half_only",
        "move": [["lib/a.py", "lib/a2.py"]],
        "targets": ["lib/a2.py"],
    },
    {"name": "unchanged_and_outside", "targets": ["lib/b.py", "@outside"]},
    {
        "name": "split_old_half_later",
        "targets": ["lib/a.py"],
    },
]

FINGERPRINTS = [
    ({}, {}),
    ({}, {"a": [1, 1.0]}),
    ({"a": [1, 1.0]}, {}),
    ({"a": [1, 1.0], "b": [2, 2.0]}, {"a": [1, 1.0], "b": [2, 2.5]}),
    ({"a": [1, 1.0], "b": [2, 2.0]}, {"a": [3, 1.0], "c": [1, 1.0]}),
]


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ragmonk_watch_golden_") as tmp:
        root = (Path(tmp) / "src_root").resolve().parent / "src_root"
        root.mkdir()
        outside = Path(tmp) / "outside.py"
        outside.write_text("O = 1\n", encoding="utf-8")
        for rel, text in TREE.items():
            _write(root, rel, text)
        root = root.resolve()
        os.environ["RAGMONK_HOME"] = str(Path(tmp) / "home")
        os.environ["RAGMONK_UPDATES__ENABLED"] = "false"

        from ragmonk.core import paths
        from ragmonk.core.lifecycle import AppContext
        from ragmonk.indexing.coordinator import ScanRequest
        from ragmonk.indexing.runner import build_processor_registry, run_source_pass
        from ragmonk.sources.registry import SourceRegistry
        from ragmonk.storage.repositories import files_repo
        from ragmonk.watcher.network import diff

        steps = []
        with AppContext.bootstrap() as ctx:
            registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
            source = registry.add(str(root))
            processors = build_processor_registry(ctx.config)
            run_source_pass(ctx, source, processors)
            project_id = paths.project_id_for_path(Path(source.path))

            def files() -> list[list[str]]:
                conn = ctx.project_conn(project_id, control_plane=True)
                return sorted(
                    [Path(f.path).relative_to(root).as_posix(), str(f.status)]
                    for f in files_repo.list_by_source(conn, source.id)
                )

            for step in STEPS:
                for rel, text in step.get("write", {}).items():
                    _write(root, rel, text)
                for rel in step.get("delete", []):
                    (root / rel).unlink()
                for src, dst in step.get("move", []):
                    shutil.move(root / src, root / dst)
                changed = frozenset(
                    str(outside) if t == "@outside" else str(root / t) for t in step["targets"]
                )
                r = run_source_pass(
                    ctx,
                    registry.get(source.id),
                    processors,
                    scan_request=ScanRequest(
                        source_id=source.id,
                        reason="local_watcher",
                        changed_paths=changed,
                        full=False,
                    ),
                ).result
                steps.append(
                    {
                        **{k: v for k, v in step.items()},
                        "counts": {
                            k: getattr(r, k)
                            for k in ("scanned", "new", "changed", "unchanged", "deleted", "moved")
                        },
                        "targeted": r.targeted,
                        "files": files(),
                    }
                )

        net = [
            {
                "previous": p,
                "current": c,
                "changed": sorted(
                    diff(
                        {k: tuple(v) for k, v in p.items()},
                        {k: tuple(v) for k, v in c.items()},
                    )
                ),
            }
            for p, c in FINGERPRINTS
        ]
    golden = {"tree": TREE, "targeted": steps, "network_diff": net}
    OUT.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
