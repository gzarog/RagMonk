"""Search Quality Improvement Plan, Phase 4: end-to-end proof that
row-aware table indexing (``documents/table_renderer.py``) actually fixes
the regression flattening caused -- indexed through the real CLI
(``ragpilot source add`` / ``ragpilot index``), queried through
``documents_repo.search_fts`` so assertions can inspect the exact FTS
``body`` text a hit carries, not just whether a hit occurred.

Two fixture documents:

* ``server_status.md`` -- a small table (fits in one chunk) proving a
  hit's ``body`` preserves row/column association: the row containing
  "85%" is the *same* line as "api-02" and "16GB", not scattered across
  an unrelated flattened blob.
* ``fleet_status.md`` -- a table with enough rows to exceed the default
  chunking ``max_tokens`` budget, proving (a) it actually splits into
  multiple chunks, (b) every split chunk repeats the header row, and (c)
  a query for a fact that only appears in a later split still finds it,
  correctly associated with its own row rather than any other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.core import paths
from ragpilot.retrieval.lexical import _fts_query
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo
from ragpilot.storage.sqlite import connect

SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _search_fts(conn, query: str):  # noqa: ANN001, ANN201 - test helper
    """``documents_repo.search_fts`` via the same query-sanitization
    ``retrieval/lexical.py``'s real ``search()`` applies (``_fts_query``)
    -- gets this test past raw FTS5 syntax errors on '%'/'-' exactly the
    way production ``ragpilot search`` already does, while still
    returning full row ``body`` text (unlike a truncated match snippet)
    so assertions can inspect row/column association directly.
    """
    fts_query = _fts_query(query)
    assert fts_query is not None
    return documents_repo.search_fts(conn, fts_query)


# One row deliberately breaks the "every non-key column shares one value"
# pattern the rest of the fixture keeps (status/ram constant) so a term
# combination is only jointly true of exactly one row -- see
# ``test_...`` below for why that is the actual regression this phase
# fixes vs. a flattened cell blob.
_FLEET_ROW_COUNT = 60
_DEGRADED_SERVER = "srv42"


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _knowledge_conn(home: Path, source_path: Path):  # noqa: ANN201 - test helper
    project_id = paths.project_id_for_path(source_path)
    conn = connect(paths.project_db_path(project_id, home))
    apply_migrations(conn, "knowledge")
    return conn


def _write_small_table(root: Path) -> None:
    (root / "server_status.md").write_text(
        "# Server Status\n\n"
        "| Server | CPU | RAM | Status |\n"
        "| --- | --- | --- | --- |\n"
        "| api-01 | 40% | 8GB | healthy |\n"
        "| api-02 | 85% | 16GB | degraded |\n"
    )


def _write_large_table(root: Path) -> None:
    lines = ["# Fleet Status\n", "| Server | CPU | RAM | Status |", "| --- | --- | --- | --- |"]
    for i in range(_FLEET_ROW_COUNT):
        name = f"srv{i:02d}"
        if name == _DEGRADED_SERVER:
            lines.append(f"| {name} | 85% | 16GB | degraded |")
        else:
            lines.append(f"| {name} | 40% | 8GB | healthy |")
    (root / "fleet_status.md").write_text("\n".join(lines) + "\n")


@pytest.fixture
def indexed_tables(
    ragpilot_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):  # noqa: ANN201 - test fixture
    root = tmp_path / "docs_project"
    root.mkdir()
    _write_small_table(root)
    _write_large_table(root)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output

    conn = _knowledge_conn(ragpilot_home, root)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Small table: row/column association survives in the indexed body text
# ---------------------------------------------------------------------------


def test_cpu_query_resolves_to_the_row_that_actually_has_that_cpu_value(
    indexed_tables,
) -> None:
    hits = _search_fts(indexed_tables, "85%")
    assert hits, "expected at least one FTS hit for '85%'"
    bodies = [h["body"] for h in hits]
    # The row containing 85% must carry its own server name and status on
    # the same line -- a flattened cell blob would still contain "85%"
    # and "api-02" *somewhere* in the same body, but so would every other
    # row's values, with no way to tell which server actually has 85%
    # CPU. Row-aware rendering keeps them on one line together.
    assert any("api-02" in body and "85%" in body for body in bodies)
    matching = next(body for body in bodies if "api-02" in body)
    for line in matching.splitlines():
        if "api-02" in line:
            assert "85%" in line and "16GB" in line and "degraded" in line
            break
    else:
        raise AssertionError(f"no line in body contained api-02: {matching!r}")


def test_ram_value_for_a_specific_server_is_found_in_its_own_row(indexed_tables) -> None:
    hits = _search_fts(indexed_tables, "api-02")
    assert hits
    bodies = [h["body"] for h in hits]
    matching = next(body for body in bodies if "api-02" in body)
    row_line = next(line for line in matching.splitlines() if "api-02" in line)
    assert "16GB" in row_line
    # And the unrelated row's own RAM value does not leak onto this line.
    assert "8GB" not in row_line


# ---------------------------------------------------------------------------
# Large table: row-boundary splitting + header repetition
# ---------------------------------------------------------------------------


def test_large_table_is_indexed_as_multiple_chunks_each_carrying_the_header(
    indexed_tables,
) -> None:
    rows = indexed_tables.execute(
        "SELECT ds.id, ds.table_rows FROM document_sections ds "
        "JOIN documents d ON d.id = ds.document_id "
        "JOIN files f ON f.id = d.file_id "
        "WHERE ds.kind = 'table' AND f.path LIKE '%fleet_status.md'"
    ).fetchall()
    assert len(rows) > 1, "a table this large should have split into more than one chunk"

    for row in rows:
        grid = json.loads(row["table_rows"])
        assert grid[0] == ["Server", "CPU", "RAM", "Status"], (
            "every split chunk must repeat the header row at the top"
        )


def test_query_for_a_fact_only_in_a_later_split_chunk_still_finds_it(indexed_tables) -> None:
    # srv42's row (85%/16GB/degraded) is far from the top of a 60-row
    # table, so it necessarily lands in a later row-boundary split than
    # the first few servers -- proving header-repeat-on-split actually
    # ran (this chunk is independently interpretable), not merely that
    # chunking happened at all.
    hits = _search_fts(indexed_tables, _DEGRADED_SERVER)
    assert hits
    bodies = [h["body"] for h in hits]
    matching = next(body for body in bodies if _DEGRADED_SERVER in body)
    # Phase 3's search_text prepends a "Document: .../Section: ..." breadcrumb
    # ahead of the table's own rendering, so the repeated header row is no
    # longer necessarily *line 0* -- just present, proving the repeat-on-split
    # behavior ran, rather than requiring a specific position that composing
    # with Phase 3's contextualization would otherwise break.
    assert "Server | CPU | RAM | Status" in matching.splitlines()
    row_line = next(line for line in matching.splitlines() if _DEGRADED_SERVER in line)
    assert "85%" in row_line and "16GB" in row_line and "degraded" in row_line


def test_combined_query_resolves_to_the_one_row_where_both_facts_are_jointly_true(
    indexed_tables,
) -> None:
    # Every other row in the fixture is 40%/8GB/healthy -- only srv42's
    # row is 85%/16GB/degraded. A query combining "degraded" (only true
    # of srv42) with the server name must land on a chunk whose own text
    # actually contains both, on the same row -- the exact association a
    # flattened, unsplit whole-table blob (the pre-Phase-4 behavior)
    # could not distinguish from "these two facts merely both appear
    # somewhere in this enormous table".
    hits = _search_fts(indexed_tables, f"{_DEGRADED_SERVER} degraded")
    assert hits
    top_body = hits[0]["body"]
    assert _DEGRADED_SERVER in top_body and "degraded" in top_body
    row_line = next(line for line in top_body.splitlines() if _DEGRADED_SERVER in line)
    assert "degraded" in row_line
    # No other server's row leaked into this same split chunk carrying
    # the word "degraded" attached to it.
    for line in top_body.splitlines()[1:]:
        if "degraded" in line:
            assert _DEGRADED_SERVER in line
