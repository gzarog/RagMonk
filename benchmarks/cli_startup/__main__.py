"""``python -m benchmarks.cli_startup [--repeats 10] [--strict]``

Standalone runner for the CLI startup latency benchmark. ``--strict``
exits non-zero if any command misses its warm-start target (``targets.py``)
-- meant for a real developer machine, not CI; see ``targets.py``'s module
docstring for why.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from benchmarks.cli_startup import runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAGpilot CLI startup latency benchmark")
    parser.add_argument("--repeats", type=int, default=runner.DEFAULT_REPEATS)
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="RAGpilot home directory (default: a temp directory)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero if any command misses its target"
    )
    args = parser.parse_args(argv)

    home = args.home or Path(tempfile.mkdtemp(prefix="ragpilot-cli-startup-"))
    results = runner.run_all(home=home, repeats=args.repeats)
    print(runner.format_report(results))
    if args.strict and any(not r.meets_target for r in results):
        print("\nOne or more commands missed their target (--strict).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
