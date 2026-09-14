"""Architectural regression test (CLI performance improvement plan, Phase
1/2): a lightweight command -- ``version``, ``--help``, ``config --help``,
``search --help`` -- must never import Docling/torch/transformers/mcp/
openai/anthropic/usearch. Those belong to subsystems (document indexing,
MCP serving, ``ask``, semantic search) a lightweight command never
touches; loading them anyway is exactly the startup-latency regression
this plan exists to prevent, so this checks the import graph directly
rather than a timing number a shared CI runner cannot reliably gate on
(see ``benchmarks/cli_startup/targets.py``).

Runs each command in a fresh subprocess -- inspecting the current
process's ``sys.modules`` would be polluted by whatever the rest of the
test suite already imported.

Was ``xfail(strict=True)`` while ``ragpilot.cli.main``'s eager imports of
every CLI submodule (``ask``, ``index``, ``serve``, ``watch``, ``vectors``,
...) transitively loaded the full heavy stack regardless of which command
was invoked. Fixed (Phase 2) by moving each heavy import to the function
that actually needs it: ``indexing/runner.py``'s ``build_processor_registry``
(Docling, via ``documents/pipeline.py``), ``cli/serve.py``'s ``serve()``
(the ``mcp`` SDK), and ``ai/factory.py``'s ``create_provider()`` (the
``openai``/``anthropic`` SDKs, one per configured provider) -- now a plain,
passing assertion.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

HEAVY_MODULES = (
    "torch",
    "transformers",
    "docling",
    "mcp",
    "usearch",
    "openai",
    "anthropic",
)

LIGHTWEIGHT_INVOCATIONS = (
    ["version"],
    ["--help"],
    ["config", "--help"],
    ["search", "--help"],
)


_MARKER = "HEAVY_MODULES:"


def _loaded_heavy_modules(cli_args: list[str]) -> list[str]:
    # `--help`/usage output (real commas included, e.g. in argument help
    # text) also lands on stdout, so the heavy-modules line is prefixed
    # with a marker and picked out by exact line match rather than naively
    # parsed out of the whole captured stdout.
    script = (
        "import sys\n"
        "from ragpilot.cli.main import app\n"
        "try:\n"
        f"    app({cli_args!r})\n"
        "except SystemExit:\n"
        "    pass\n"
        f"heavy = {list(HEAVY_MODULES)!r}\n"
        f"print({_MARKER!r} + ','.join(sorted(m for m in heavy if m in sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    for line in result.stdout.splitlines():
        if line.startswith(_MARKER):
            loaded = line[len(_MARKER) :]
            return loaded.split(",") if loaded else []
    raise AssertionError(
        f"{cli_args}: subprocess produced no {_MARKER!r} marker line "
        f"(rc={result.returncode}); stderr:\n{result.stderr}"
    )


@pytest.mark.parametrize("cli_args", LIGHTWEIGHT_INVOCATIONS, ids=lambda a: " ".join(a))
def test_lightweight_command_does_not_load_heavy_dependencies(cli_args: list[str]) -> None:
    loaded = _loaded_heavy_modules(cli_args)
    assert loaded == [], f"{cli_args} loaded heavy modules: {loaded}"
