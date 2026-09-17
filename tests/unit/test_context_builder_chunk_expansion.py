"""``retrieval/context_builder.py``'s ``expand_chunk_context`` -- Search
Quality Improvement Plan Phase 9: attaching a matched chunk's nearest
parent heading and previous/next sibling chunks as a post-ranking
presentation step, never as a ranking input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragpilot.core.config import SearchContextConfig
from ragpilot.core.models import (
    Document,
    DocumentFormat,
    FileKind,
    FileRecord,
    FileStatus,
    Paragraph,
    Section,
)
from ragpilot.retrieval.context_builder import expand_chunk_context
from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import documents_repo
from ragpilot.storage.repositories.files_repo import insert as insert_file
from ragpilot.storage.sqlite import connect, transaction

_DEFAULT_CONFIG = SearchContextConfig(
    parent_heading=True, previous_chunks=1, next_chunks=1, max_tokens=1200
)


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201 - test fixture
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    insert_file(
        connection,
        FileRecord(
            id="f1",
            source_id="s1",
            path="/docs/report.md",
            kind=FileKind.DOCUMENT,
            size=10,
            mtime=0.0,
            status=FileStatus.QUEUED,
            created_at="now",
            updated_at="now",
        ),
    )
    with transaction(connection):
        documents_repo.insert_document(
            connection,
            Document(
                id="d1",
                source_id="s1",
                file_id="f1",
                format=DocumentFormat.MARKDOWN,
                title="Report",
                section_count=1,
                paragraph_count=3,
                generation=1,
                created_at="now",
                updated_at="now",
            ),
        )
        documents_repo.insert_section(
            connection,
            Section(
                id="h1",
                document_id="d1",
                file_id="f1",
                heading_level=1,
                text="Results",
                heading_path=["Results"],
                parent_id=None,
                order_index=0,
                generation=1,
                created_at="now",
            ),
            doc_title="Report",
        )
        for index, (chunk_id, text) in enumerate(
            [
                ("p1", "First paragraph text."),
                ("p2", "Second paragraph text, the one that matches."),
                ("p3", "Third paragraph text."),
            ],
            start=1,
        ):
            documents_repo.insert_paragraph(
                connection,
                Paragraph(
                    id=chunk_id,
                    document_id="d1",
                    file_id="f1",
                    text=text,
                    heading_path=["Results"],
                    parent_id="h1",
                    order_index=index,
                    generation=1,
                    created_at="now",
                ),
                doc_title="Report",
            )
    yield connection
    connection.close()


def test_matched_chunk_gets_parent_heading_and_both_siblings(conn) -> None:  # noqa: ANN001
    result = expand_chunk_context(conn, "p2", config=_DEFAULT_CONFIG)

    assert result is not None
    assert result.matched.id == "p2"
    assert result.matched.kind == "paragraph"
    assert result.parent_heading is not None
    assert result.parent_heading.id == "h1"
    assert [p.id for p in result.previous] == ["p1"]
    assert [p.id for p in result.next] == ["p3"]
    assert result.truncated is False


def test_first_chunk_has_no_previous_sibling(conn) -> None:  # noqa: ANN001
    result = expand_chunk_context(conn, "p1", config=_DEFAULT_CONFIG)

    assert result is not None
    assert result.previous == []
    assert [p.id for p in result.next] == ["p2"]


def test_last_chunk_has_no_next_sibling(conn) -> None:  # noqa: ANN001
    result = expand_chunk_context(conn, "p3", config=_DEFAULT_CONFIG)

    assert result is not None
    assert [p.id for p in result.previous] == ["p2"]
    assert result.next == []


def test_unknown_chunk_id_returns_none(conn) -> None:  # noqa: ANN001
    assert expand_chunk_context(conn, "does-not-exist", config=_DEFAULT_CONFIG) is None


def test_parent_heading_false_disables_it(conn) -> None:  # noqa: ANN001
    config = SearchContextConfig(
        parent_heading=False, previous_chunks=1, next_chunks=1, max_tokens=1200
    )
    result = expand_chunk_context(conn, "p2", config=config)

    assert result is not None
    assert result.parent_heading is None
    assert [p.id for p in result.previous] == ["p1"]
    assert [p.id for p in result.next] == ["p3"]


def test_zero_previous_chunks_disables_previous_only(conn) -> None:  # noqa: ANN001
    config = SearchContextConfig(
        parent_heading=True, previous_chunks=0, next_chunks=1, max_tokens=1200
    )
    result = expand_chunk_context(conn, "p2", config=config)

    assert result is not None
    assert result.previous == []
    assert [p.id for p in result.next] == ["p3"]
    assert result.parent_heading is not None


def test_zero_next_chunks_disables_next_only(conn) -> None:  # noqa: ANN001
    config = SearchContextConfig(
        parent_heading=True, previous_chunks=1, next_chunks=0, max_tokens=1200
    )
    result = expand_chunk_context(conn, "p2", config=config)

    assert result is not None
    assert [p.id for p in result.previous] == ["p1"]
    assert result.next == []


def test_all_pieces_disabled_returns_just_the_matched_chunk(conn) -> None:  # noqa: ANN001
    config = SearchContextConfig(
        parent_heading=False, previous_chunks=0, next_chunks=0, max_tokens=1200
    )
    result = expand_chunk_context(conn, "p2", config=config)

    assert result is not None
    assert result.matched.id == "p2"
    assert result.parent_heading is None
    assert result.previous == []
    assert result.next == []
    assert result.truncated is False


def test_tight_budget_trims_siblings_but_keeps_the_matched_chunk(conn) -> None:  # noqa: ANN001
    # The matched chunk's own text ("Second paragraph text, the one that
    # matches.") already costs a handful of tokens; a budget just above
    # that leaves no room for the parent heading or either sibling, but
    # must never drop the matched chunk itself.
    tight_config = SearchContextConfig(
        parent_heading=True, previous_chunks=1, next_chunks=1, max_tokens=6
    )

    result = expand_chunk_context(conn, "p2", config=tight_config)

    assert result is not None
    assert result.matched.id == "p2"
    assert result.matched.text  # never trimmed away
    assert result.parent_heading is None
    assert result.previous == []
    assert result.next == []
    assert result.truncated is True
    assert any("max_tokens" in reason for reason in result.truncation_reasons)


def test_budget_between_extremes_keeps_parent_and_previous_drops_next(conn) -> None:  # noqa: ANN001
    # matched=13, "Results"=2, "First paragraph text."=7, "Third
    # paragraph text."=7 tokens (see the module's own count_tokens) --
    # a budget of 22 fits matched+parent+previous (13+2+7=22) but not
    # +next (29), so next is what gets trimmed, never the matched chunk
    # or the (higher-priority) parent heading.
    config = SearchContextConfig(
        parent_heading=True, previous_chunks=1, next_chunks=1, max_tokens=22
    )

    result = expand_chunk_context(conn, "p2", config=config)

    assert result is not None
    assert result.parent_heading is not None
    assert [p.id for p in result.previous] == ["p1"]
    assert result.next == []
    assert result.truncated is True


def test_roomy_budget_never_reports_truncation(conn) -> None:  # noqa: ANN001
    result = expand_chunk_context(conn, "p2", config=_DEFAULT_CONFIG)

    assert result is not None
    assert result.truncated is False
    assert result.truncation_reasons == []


def test_to_dict_structurally_separates_matched_from_context(conn) -> None:  # noqa: ANN001
    result = expand_chunk_context(conn, "p2", config=_DEFAULT_CONFIG)
    assert result is not None

    payload = result.to_dict()

    # The matched hit lives under its own key, distinct from every piece
    # of surrounding context -- a consumer never has to infer which item
    # in a flat list was the actual match.
    assert payload["matched"]["id"] == "p2"
    assert payload["parent_heading"]["id"] == "h1"
    assert [p["id"] for p in payload["previous"]] == ["p1"]
    assert [p["id"] for p in payload["next"]] == ["p3"]
    assert "p2" not in [p["id"] for p in payload["previous"]] + [p["id"] for p in payload["next"]]
