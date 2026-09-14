"""``python -m benchmarks.search_quality [--size ...] [--repeats N] [--out DIR]``

Thin entrypoint over ``report.main`` -- see that module's docstring.
"""

from __future__ import annotations

from benchmarks.search_quality.report import main

if __name__ == "__main__":
    raise SystemExit(main())
