"""Phase 8 operations: backup, restore, rebuild, and upgrade.

Each module here orchestrates *existing* machinery (SQLite's own online
backup API, ``storage/migrations.py``'s idempotent migration system,
``indexing/runner.py``'s per-source indexing pass, ``service/pid.py``'s
daemon detection) rather than inventing a parallel one -- this package is
glue and safety-ordering, not a second storage or indexing layer. Kept
separate from ``cli/`` so the orchestration logic is testable without
going through Typer, mirroring how ``indexing/runner.py`` sits underneath
both ``cli/index.py`` and ``service/daemon.py``.
"""

from __future__ import annotations
