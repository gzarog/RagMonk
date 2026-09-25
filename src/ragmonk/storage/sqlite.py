"""SQLite connection factory and transaction helper.

WAL + a busy timeout let a reader and the single writer coexist without
"database is locked" errors under normal CLI usage; NORMAL synchronous is
the standard WAL pairing (still durable across app crashes, only an OS
crash can lose the last commit) traded for far less fsync overhead.
``temp_store = MEMORY`` keeps FTS5/sort/join scratch space (e.g. a large
``ORDER BY``) off disk, and a configurable page cache (blueprint section
22) trades RAM for fewer page reads on repeated queries against the same
database.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ragmonk.core.errors import DatabaseError


def connect(db_path: Path, *, cache_size_mb: int = 64) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # check_same_thread=False: Phase 7's daemon (service/daemon.py)
        # bootstraps one AppContext on its main thread but then reuses
        # its connections from a dedicated worker thread and a
        # reconciliation thread, serialized through its own lock rather
        # than one thread each -- sqlite3's default same-thread check has
        # nothing to do with that serialization, only with which OS
        # thread created the connection, so it would reject the reuse
        # outright. Every other (single-threaded) caller is unaffected.
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    except sqlite3.Error as exc:
        raise DatabaseError(f"failed to open database {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store = MEMORY")
    # Negative cache_size is in KiB (SQLite: "approximately abs(N*1024)
    # bytes"), not pages -- so this is a size in MB, not a page count.
    conn.execute(f"PRAGMA cache_size = -{cache_size_mb * 1024}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@dataclass
class StatementCounters:
    """Indexing optimization plan V2, Phase P4: a benchmark-only record
    of every SQL statement executed against one connection during the
    enclosed block, plus the wall time spent inside it -- see
    ``count_statements``'s docstring. Statements are counted, not
    individually timed: ``sqlite3.Connection.set_trace_callback`` fires
    with the SQL text alone, and timing each statement's own execution
    separately would need wrapping every call site, real overhead this
    phase's own "lightweight, not always-on" requirement rules out. The
    per-block ``total_seconds`` this dataclass already carries is what
    every actual before/after measurement in this phase's commit message
    is based on.
    """

    count: int = 0
    total_seconds: float = 0.0
    statements: list[str] = field(default_factory=list)


@contextmanager
def count_statements(
    conn: sqlite3.Connection, *, keep_text: bool = False
) -> Iterator[StatementCounters]:
    """Benchmark/diagnostic-only: counts every SQL statement SQLite
    actually executes against ``conn`` for the enclosed block (via
    ``sqlite3.Connection.set_trace_callback``), and times the block as a
    whole. Zero cost when not used -- ``connect()`` never installs a
    trace callback itself, so a normal ``ragmonk index``/daemon run pays
    nothing for this existing. Restores whatever trace callback (if any)
    ``conn`` had installed. The stdlib ``sqlite3`` module exposes no way
    to *read back* a connection's currently installed trace callback, so
    this cannot detect or restore a prior one -- clears to no callback
    (``None``) on exit unconditionally. Every caller in this project only
    ever uses this on a connection with no trace callback already
    installed (a benchmark script, or a focused test), so this is not a
    real-world limitation today, just an honest description of what
    ``set_trace_callback`` actually allows.

    ``keep_text=True`` also collects each statement's raw SQL text (for
    ad hoc inspection, e.g. finding *which* query ran N times) --
    off by default since retaining every statement's text is real, if
    small, memory overhead a plain statement-count measurement doesn't
    need.

    Used to produce every before/after statement-count number in the
    indexing optimization plan V2, Phase P4 commit -- see
    ``tests/unit/test_sqlite_statement_counting.py`` and that phase's
    commit message for how.
    """
    counters = StatementCounters()

    def _on_statement(sql: str) -> None:
        counters.count += 1
        if keep_text:
            counters.statements.append(sql)

    conn.set_trace_callback(_on_statement)
    started = time.perf_counter()
    try:
        yield counters
    finally:
        counters.total_seconds = time.perf_counter() - started
        conn.set_trace_callback(None)
