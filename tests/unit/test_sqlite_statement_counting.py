"""Indexing optimization plan V2, Phase P4: the optional, benchmark-only
SQL statement counter (``storage/sqlite.count_statements``) used to
produce this phase's before/after measurements.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect, count_statements, transaction


def test_counts_exactly_the_statements_executed_in_the_block(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        with count_statements(conn) as counters:
            conn.execute("SELECT 1")
            conn.execute("SELECT 2")
            conn.execute("SELECT 3")
        assert counters.count == 3
        assert counters.total_seconds >= 0.0
        assert counters.statements == []  # keep_text defaults to off
    finally:
        conn.close()


def test_keep_text_collects_the_raw_sql(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        with count_statements(conn, keep_text=True) as counters:
            conn.execute("SELECT 42")
        assert counters.count == 1
        assert counters.statements == ["SELECT 42"]
    finally:
        conn.close()


def test_statements_outside_the_block_are_never_counted(tmp_path: Path) -> None:
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        conn.execute("SELECT 'before'")
        with count_statements(conn) as counters:
            conn.execute("SELECT 'inside'")
        conn.execute("SELECT 'after'")
        assert counters.count == 1
    finally:
        conn.close()


def test_executemany_counts_one_statement_per_row(tmp_path: Path) -> None:
    """Confirms this phase's own finding about ``executemany``: SQLite's
    trace callback fires once per bound row execution, not once for the
    whole call -- exactly what made batching INSERTs via ``executemany``
    show no statement-count reduction (see this phase's commit message)
    the way an ``IN (...)``-clause DELETE/UPDATE genuinely does.
    """
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        with transaction(conn), count_statements(conn) as counters:
            conn.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [(f"k{i}", f"v{i}") for i in range(5)],
            )
        assert counters.count == 5
    finally:
        conn.close()


def test_a_multi_row_in_clause_statement_counts_as_one(tmp_path: Path) -> None:
    """The contrasting case: one ``DELETE ... WHERE x IN (...)`` covering
    several rows is genuinely one statement, regardless of how many rows
    it matches -- this is the real, measured win this phase kept.
    """
    conn = connect(tmp_path / "k.db")
    try:
        apply_migrations(conn, "knowledge")
        with transaction(conn):
            conn.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [(f"k{i}", f"v{i}") for i in range(5)],
            )
        with transaction(conn), count_statements(conn) as counters:
            conn.execute(
                "DELETE FROM metadata WHERE key IN (?, ?, ?, ?, ?)",
                ("k0", "k1", "k2", "k3", "k4"),
            )
        assert counters.count == 1
    finally:
        conn.close()
