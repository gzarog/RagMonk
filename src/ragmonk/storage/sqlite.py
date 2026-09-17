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
from collections.abc import Iterator
from contextlib import contextmanager
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
