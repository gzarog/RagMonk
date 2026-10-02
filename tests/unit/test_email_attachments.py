"""EML attachment knowledge extraction V1: MIME enumeration (P0),
attachment conversion through the existing document pipeline (P1), and
``prepare_document``/``publish_document`` producing parent + child
documents (P2-P4), plus the ``.eml``-scoped version stamp.
"""

from __future__ import annotations

import base64
import shutil
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pytest

from ragmonk.core.models import DocumentFormat, FileKind
from ragmonk.documents import attachment_pipeline, docling_adapter
from ragmonk.documents import pipeline as doc_pipeline
from ragmonk.documents.attachment_pipeline import AttachmentSkipped
from ragmonk.documents.email_attachments import (
    SKIP_COUNT_LIMIT,
    SKIP_EMPTY,
    SKIP_NESTED_MESSAGE,
    SKIP_TOO_LARGE,
    SKIP_TOTAL_LIMIT,
    SKIP_UNSUPPORTED,
    AttachmentPart,
    EmailAttachmentSettings,
    extract_attachments,
    sanitize_filename,
)
from ragmonk.indexing.coordinator import ProcessorContext
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"


def _email(body: str = "Body text.") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    msg["Subject"] = "Subject line"
    msg.set_content(body)
    return msg


# -- P0: MIME enumeration ----------------------------------------------------


def test_plain_email_has_no_attachments() -> None:
    result = extract_attachments(FIXTURES / "sample.eml")
    assert result.attachments == ()
    assert result.skipped == ()


def test_multipart_alternative_body_is_not_an_attachment() -> None:
    msg = _email("plain body")
    msg.add_alternative("<p>html body</p>", subtype="html")
    assert extract_attachments(msg.as_bytes()).attachments == ()


def test_base64_and_quoted_printable_attachments_decode() -> None:
    msg = _email()
    msg.add_attachment(
        b"\x00\x01binary\xff", maintype="application", subtype="pdf", filename="a.pdf"
    )
    msg.add_attachment(
        "café quoted text\n", subtype="plain", filename="b.txt", cte="quoted-printable"
    )
    raw = msg.as_bytes()
    assert b"Content-Transfer-Encoding: base64" in raw
    assert b"quoted-printable" in raw
    parts = extract_attachments(raw).attachments
    assert [p.filename for p in parts] == ["a.pdf", "b.txt"]
    assert parts[0].payload == b"\x00\x01binary\xff"
    assert parts[1].payload.decode("utf-8") == "café quoted text\n"
    assert parts[0].decoded_size == len(b"\x00\x01binary\xff")


def test_unicode_rfc2231_filename_decodes() -> None:
    parts = extract_attachments(FIXTURES / "email_with_attachments.eml").attachments
    names = [p.filename for p in parts]
    assert "Résumé – memo.md" in names


def test_rfc2047_encoded_filename_decodes() -> None:
    encoded = "=?utf-8?b?" + base64.b64encode("ρ-report.txt".encode()).decode() + "?="
    raw = (
        "From: a@example.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        f'--B\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename="{encoded}"'
        "\r\n\r\nattached\r\n--B--\r\n"
    ).encode()
    (part,) = extract_attachments(raw).attachments
    assert part.filename == "ρ-report.txt"


def test_duplicate_filenames_stay_distinct_by_ordinal() -> None:
    parts = extract_attachments(FIXTURES / "email_with_duplicate_names.eml").attachments
    assert [p.filename for p in parts] == ["notes.txt", "notes.txt"]
    assert [p.ordinal for p in parts] == [0, 1]
    assert parts[0].payload != parts[1].payload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../x.pdf", "x.pdf"),
        ("..\\..\\windows\\evil.docx", "evil.docx"),
        ("/etc/passwd", "passwd"),
        ("..", None),
        ("", None),
        ("bad\x00name\x07.txt", "badname.txt"),
        (None, None),
    ],
)
def test_sanitize_filename_never_yields_a_path(raw: str | None, expected: str | None) -> None:
    assert sanitize_filename(raw) == expected


def test_malicious_filename_is_display_only() -> None:
    msg = _email()
    msg.add_attachment("x", subtype="plain", filename="../../x.txt")
    (part,) = extract_attachments(msg.as_bytes()).attachments
    assert part.filename == "x.txt"
    assert "/" not in part.display_name


def test_ordering_is_deterministic() -> None:
    raw = (FIXTURES / "email_with_attachments.eml").read_bytes()
    first = [(p.ordinal, p.filename) for p in extract_attachments(raw).attachments]
    second = [(p.ordinal, p.filename) for p in extract_attachments(raw).attachments]
    assert first == second
    assert [o for o, _ in first] == sorted(o for o, _ in first)


def test_nested_message_is_skipped_not_recursed() -> None:
    inner = _email("inner body with secretinner word")
    inner.add_attachment("inner attachment", subtype="plain", filename="inner.txt")
    outer = _email()
    outer.add_attachment(inner)
    result = extract_attachments(outer.as_bytes())
    assert result.attachments == ()
    assert [s.reason for s in result.skipped] == [SKIP_NESTED_MESSAGE]


def test_empty_attachment_is_skipped_with_reason() -> None:
    msg = _email()
    msg.add_attachment(b"", maintype="application", subtype="pdf", filename="empty.pdf")
    result = extract_attachments(msg.as_bytes())
    assert result.attachments == ()
    assert result.skipped[0].reason == SKIP_EMPTY


def test_size_count_and_total_limits() -> None:
    msg = _email()
    for i in range(4):
        msg.add_attachment(b"x" * 10, maintype="text", subtype="plain", filename=f"{i}.txt")
    msg.add_attachment(b"y" * 100, maintype="text", subtype="plain", filename="big.txt")
    raw = msg.as_bytes()

    too_large = extract_attachments(raw, EmailAttachmentSettings(max_bytes=50))
    assert [s.reason for s in too_large.skipped] == [SKIP_TOO_LARGE]
    assert len(too_large.attachments) == 4

    count = extract_attachments(raw, EmailAttachmentSettings(max_count=2))
    assert len(count.attachments) == 2
    assert [s.reason for s in count.skipped] == [SKIP_COUNT_LIMIT] * 3

    total = extract_attachments(raw, EmailAttachmentSettings(total_max_bytes=25))
    assert len(total.attachments) == 2
    assert {s.reason for s in total.skipped} == {SKIP_TOTAL_LIMIT}


# -- P1: conversion through the existing pipeline ------------------------------


def _part(filename: str | None, content_type: str, payload: bytes = b"data") -> AttachmentPart:
    return AttachmentPart(0, filename, content_type, None, payload)


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("a.PDF", "application/octet-stream", DocumentFormat.PDF),
        ("report.docx", "application/octet-stream", DocumentFormat.DOCX),
        (None, "application/pdf", DocumentFormat.PDF),
        ("noext", "text/plain", DocumentFormat.TXT),
        ("archive.zip", "application/pdf", None),  # explicit extension wins
        (None, "application/octet-stream", None),
        ("macro.docm", "application/octet-stream", None),
    ],
)
def test_attachment_format_selection(
    filename: str | None, content_type: str, expected: DocumentFormat | None
) -> None:
    assert attachment_pipeline.attachment_format(_part(filename, content_type)) == expected


def _prepare(part: AttachmentPart) -> attachment_pipeline.PreparedAttachment:
    return attachment_pipeline.prepare_attachment(
        part,
        cache_conn=None,
        ocr_mode="off",
        image_ocr=False,
        chunking=None,
        max_document_pages=None,
    )


def test_unsupported_zip_is_skipped() -> None:
    with pytest.raises(AttachmentSkipped) as exc:
        _prepare(_part("archive.zip", "application/zip"))
    assert exc.value.reason == SKIP_UNSUPPORTED


def test_txt_attachment_converts_with_attachment_title() -> None:
    prepared = _prepare(_part("notes.txt", "text/plain", b"Zebrafish migration notes."))
    assert prepared.doc_format is DocumentFormat.TXT
    assert prepared.meta.title == "notes.txt"
    assert any("Zebrafish" in c.text for c in prepared.chunks)


def test_docx_attachment_matches_standalone_conversion(tmp_path: Path) -> None:
    payload = (FIXTURES / "document.docx").read_bytes()
    prepared = _prepare(_part("document.docx", "application/octet-stream", payload))
    standalone = tmp_path / "document.docx"
    shutil.copy(FIXTURES / "document.docx", standalone)
    ctx = ProcessorContext(
        path=standalone,
        size=standalone.stat().st_size,
        kind=FileKind.DOCUMENT,
        max_size_bytes=10**9,
    )
    reference = doc_pipeline.prepare_document(ctx, cache_conn=None)
    assert reference.chunks is not None
    assert [(c.kind, c.text) for c in prepared.chunks] == [
        (c.kind, c.text) for c in reference.chunks
    ]


def _tempdirs() -> set[str]:
    import tempfile

    return {p.name for p in Path(tempfile.gettempdir()).glob("ragmonk-attachment-*")}


def test_temporary_files_removed_on_success_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _tempdirs()
    _prepare(_part("notes.txt", "text/plain", b"hello"))
    assert _tempdirs() == before

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(docling_adapter, "convert", boom)
    with pytest.raises(RuntimeError):
        _prepare(_part("notes.txt", "text/plain", b"hello"))
    assert _tempdirs() == before


# -- P2-P4: prepare/publish parent + attachment children ---------------------


@pytest.fixture
def knowledge_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "knowledge.db")
    apply_migrations(conn, "knowledge")
    conn.execute(
        "INSERT INTO files (id, source_id, path, kind, size, mtime, status, created_at, "
        "updated_at) VALUES ('f1', 's1', 'x', 'document', 1, 1, 'pending', 'now', 'now')"
    )
    conn.commit()
    return conn


def _ctx(
    path: Path,
    conn: sqlite3.Connection | None = None,
    *,
    settings: EmailAttachmentSettings | None = None,
    generation: int = 1,
) -> ProcessorContext:
    return ProcessorContext(
        path=path,
        size=path.stat().st_size,
        kind=FileKind.DOCUMENT,
        max_size_bytes=10**9,
        conn=conn,
        source_id="s1",
        file_id="f1",
        next_generation=generation,
        email_attachments=settings,
    )


def _copy(name: str, tmp_path: Path, as_name: str = "mail.eml") -> Path:
    target = tmp_path / as_name
    shutil.copy(FIXTURES / name, target)
    return target


def test_prepare_document_disabled_is_body_only(tmp_path: Path) -> None:
    path = _copy("email_with_attachments.eml", tmp_path)
    for settings in (None, EmailAttachmentSettings(enabled=False)):
        prepared = doc_pipeline.prepare_document(_ctx(path, settings=settings), cache_conn=None)
        assert prepared.attachments == ()
        assert prepared.attachment_stats is None
        assert prepared.meta is not None and prepared.meta.title == "Quarterly planning"


def test_prepare_document_converts_supported_attachments(tmp_path: Path) -> None:
    path = _copy("email_with_attachments.eml", tmp_path)
    prepared = doc_pipeline.prepare_document(
        _ctx(path, settings=EmailAttachmentSettings()), cache_conn=None
    )
    by_name = {a.name: a for a in prepared.attachments}
    assert set(by_name) == {"notes.txt", "report.docx", "budget.xlsx", "Résumé – memo.md"}
    assert by_name["report.docx"].doc_format is DocumentFormat.DOCX
    assert by_name["budget.xlsx"].doc_format is DocumentFormat.XLSX
    stats = prepared.attachment_stats
    assert stats is not None
    assert (stats.seen, stats.indexed, stats.skipped, stats.failed) == (5, 4, 1, 0)
    assert stats.diagnostics[0].filename == "archive.zip"
    # The email body itself is unchanged by attachment extraction.
    assert prepared.chunks is not None
    assert all("zebrafish" not in c.text for c in prepared.chunks)


def test_corrupt_attachment_is_reported_and_email_still_indexed(
    tmp_path: Path, knowledge_conn: sqlite3.Connection
) -> None:
    path = _copy("email_with_corrupt_attachment.eml", tmp_path)
    ctx = _ctx(path, knowledge_conn, settings=EmailAttachmentSettings())
    prepared = doc_pipeline.prepare_document(ctx, cache_conn=None)
    assert prepared.attachment_stats is not None
    assert prepared.attachment_stats.failed == 1
    assert [a.name for a in prepared.attachments] == ["ok.txt"]
    outcome = doc_pipeline.publish_document(ctx, prepared)
    assert outcome.status.value == "indexed"
    assert outcome.attachments_failed == 1
    assert outcome.attachments_indexed == 1
    parent = documents_repo.get_document_by_file(knowledge_conn, "f1")
    assert parent is not None and parent.parent_document_id is None


def test_publish_writes_parent_and_children_and_reindex_drops_stale(
    tmp_path: Path, knowledge_conn: sqlite3.Connection
) -> None:
    path = _copy("email_with_attachments.eml", tmp_path)
    settings = EmailAttachmentSettings()
    ctx = _ctx(path, knowledge_conn, settings=settings)
    doc_pipeline.publish_document(ctx, doc_pipeline.prepare_document(ctx, cache_conn=None))

    docs = documents_repo.list_all(knowledge_conn)
    assert len(docs) == 5  # email + 4 supported attachments
    parent = documents_repo.get_document_by_file(knowledge_conn, "f1")
    assert parent is not None
    children = documents_repo.list_attachments(knowledge_conn, parent.id)
    assert [c.attachment_name for c in children] == [
        "notes.txt",
        "report.docx",
        "budget.xlsx",
        "Résumé – memo.md",
    ]
    assert all(c.file_id == "f1" and c.generation == 1 for c in children)
    assert children[0].attachment_content_type == "text/plain"
    assert [c.attachment_index for c in children] == [0, 1, 2, 4]

    hits = documents_repo.search_fts_projection(knowledge_conn, "quokkaledger")
    assert hits and hits[0].attachment is not None
    assert hits[0].attachment["name"] == "report.docx"
    assert hits[0].attachment["parent_title"] == "Quarterly planning"
    assert hits[0].path == "x"  # the real parent file path, never fabricated
    # Unit listing per document stays scoped to that document.
    assert all(
        u.document_id == parent.id
        for u in documents_repo.list_units_by_document(knowledge_conn, parent.id)
    )

    # Reindex the same file with only one attachment left.
    msg = _email("now just one")
    msg.add_attachment("narwhal only\n", subtype="plain", filename="left.txt")
    path.write_bytes(msg.as_bytes())
    ctx2 = _ctx(path, knowledge_conn, settings=settings, generation=2)
    doc_pipeline.publish_document(ctx2, doc_pipeline.prepare_document(ctx2, cache_conn=None))
    assert len(documents_repo.list_all(knowledge_conn)) == 2
    assert documents_repo.search_fts_projection(knowledge_conn, "quokkaledger") == []
    assert documents_repo.search_fts_projection(knowledge_conn, "narwhal")


def test_version_stamp_is_scoped_to_eml_when_enabled() -> None:
    base = doc_pipeline.document_version_stamp(Path("a.pdf"), email_attachments=True)
    eml_on = doc_pipeline.document_version_stamp(Path("a.eml"), email_attachments=True)
    eml_off = doc_pipeline.document_version_stamp(Path("a.eml"), email_attachments=False)
    assert base.parser_version == docling_adapter.parser_version()
    assert eml_off.parser_version == docling_adapter.parser_version()
    assert eml_on.parser_version.endswith(doc_pipeline.EMAIL_ATTACHMENTS_VERSION)
    assert eml_on.chunker_version == base.chunker_version


@pytest.mark.docling_pdf
def test_pdf_attachment_converts_like_standalone_pdf() -> None:
    payload = (FIXTURES / "sample.pdf").read_bytes()
    prepared = _prepare(_part("scan.pdf", "application/pdf", payload))
    assert prepared.doc_format is DocumentFormat.PDF
    assert prepared.chunks
