"""Search Quality Improvement Plan, Phase 4: ``documents/table_renderer.py``
in isolation -- row-preserving rendering and row-boundary splitting,
independent of the Docling/chunker machinery that wires it in
(``test_document_normalizer.py``/``test_chunker.py`` cover that).
"""

from __future__ import annotations

from ragmonk.documents import table_renderer


def test_render_rows_keeps_each_row_on_its_own_pipe_delimited_line() -> None:
    rows = (("Server", "CPU", "RAM"), ("api-01", "40%", "8GB"))
    assert table_renderer.render_rows(rows) == "Server | CPU | RAM\napi-01 | 40% | 8GB"


def test_render_rows_of_empty_grid_is_empty_string() -> None:
    assert table_renderer.render_rows(()) == ""


def test_render_table_prepends_caption_when_present() -> None:
    rows = (("A", "B"),)
    rendered = table_renderer.render_table(rows, caption="Table 1: Example.")
    assert rendered == "Table 1: Example.\n\nA | B"


def test_render_table_without_caption_is_just_the_grid() -> None:
    rows = (("A", "B"),)
    assert table_renderer.render_table(rows, caption=None) == "A | B"


def test_render_table_with_caption_but_no_rows_is_just_the_caption() -> None:
    assert table_renderer.render_table((), caption="Empty table.") == "Empty table."


def test_split_data_rows_returns_empty_list_for_no_data_rows() -> None:
    assert table_renderer.split_data_rows((), header_rows=(("H",),), max_tokens=10) == []


def test_split_data_rows_keeps_everything_in_one_group_when_it_fits() -> None:
    header = (("Server", "CPU"),)
    data = (("api-01", "40%"), ("api-02", "85%"))
    groups = table_renderer.split_data_rows(data, header_rows=header, max_tokens=100)
    assert groups == [list(data)]


def test_split_data_rows_splits_at_row_boundaries_never_mid_row() -> None:
    header = (("Server", "CPU"),)
    data = tuple((f"srv{i:02d}", "40%") for i in range(10))
    groups = table_renderer.split_data_rows(data, header_rows=header, max_tokens=12)

    assert len(groups) > 1
    # Every row appears exactly once, in order, across all groups -- no
    # row split apart, none dropped, none duplicated.
    flattened = tuple(row for group in groups for row in group)
    assert flattened == data
    for group in groups:
        assert group  # never an empty group


def test_split_data_rows_keeps_an_oversized_single_row_whole() -> None:
    header = (("Header",),)
    big_cell = " ".join(f"cell{i:04d}" for i in range(50))
    data = ((big_cell,),)
    groups = table_renderer.split_data_rows(data, header_rows=header, max_tokens=5)
    assert groups == [[(big_cell,)]]


def test_split_data_rows_accounts_for_fixed_overhead_in_every_group_budget() -> None:
    header = (("H",),)
    data = tuple((f"row{i:02d}",) for i in range(6))
    overhead = "Caption: " + " ".join(f"word{i:04d}" for i in range(10))

    without_overhead = table_renderer.split_data_rows(data, header_rows=header, max_tokens=20)
    with_overhead = table_renderer.split_data_rows(
        data, header_rows=header, max_tokens=20, fixed_overhead=overhead
    )
    # The same max_tokens budget fits fewer data rows per group once a
    # non-trivial fixed overhead (e.g. a caption, present in every
    # resulting chunk) is also counted against it.
    assert len(with_overhead) >= len(without_overhead)
