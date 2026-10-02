"""MIME attachment enumeration for ``.eml`` files (EML attachment knowledge
extraction V1, Phase P0).

Pure stdlib (``email`` with ``policy.default``) -- no Docling, no SQLite --
so it is cheap to import from the coordinator and safe to call from any
document-extraction worker thread.

Safety rules this module enforces (see the V1 plan's security section):

* An attachment's filename is *display metadata only*. It is decoded
  (RFC 2047 / RFC 2231, via ``policy.default``) and sanitized down to a
  bare basename, but it never names anything on disk -- materialization
  (``documents/attachment_pipeline.py``) always writes under an
  internally generated name.
* Size/count/total-size limits are applied to the *decoded* payload before
  any conversion is attempted.
* ``message/rfc822`` parts (an attached email) are not recursed into in
  V1, and archives are never unpacked -- both are reported as skipped.
* Payload bytes are never logged; diagnostics carry only ordinal, name,
  content type, size and a reason code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_COUNT = 50
DEFAULT_TOTAL_MAX_BYTES = 100 * 1024 * 1024

# Reason codes for ``SkippedAttachment.reason`` -- stable strings, surfaced
# in logs and index-run diagnostics.
SKIP_EMPTY = "empty"
SKIP_TOO_LARGE = "too_large"
SKIP_COUNT_LIMIT = "count_limit"
SKIP_TOTAL_LIMIT = "total_limit"
SKIP_DECODE_ERROR = "decode_error"
SKIP_NESTED_MESSAGE = "nested_message"
SKIP_UNSUPPORTED = "unsupported_format"
SKIP_IMAGE_OCR_DISABLED = "image_ocr_disabled"
SKIP_PAGE_LIMIT = "page_limit"


@dataclass(frozen=True, slots=True)
class EmailAttachmentSettings:
    """``config.documents.email_attachment*`` threaded through
    ``ProcessorContext`` rather than read from global config in low-level
    modules. ``ProcessorContext.email_attachments is None`` (a
    coordinator-external context, e.g. most unit tests) means disabled.
    """

    enabled: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    max_count: int = DEFAULT_MAX_COUNT
    total_max_bytes: int = DEFAULT_TOTAL_MAX_BYTES

    @classmethod
    def from_config(cls, documents_config: Any) -> EmailAttachmentSettings:
        return cls(
            enabled=bool(documents_config.email_attachments),
            max_bytes=int(documents_config.email_attachment_max_bytes),
            max_count=int(documents_config.email_attachment_max_count),
            total_max_bytes=int(documents_config.email_attachment_total_max_bytes),
        )


@dataclass(frozen=True, slots=True)
class AttachmentPart:
    """One decoded MIME attachment. ``ordinal`` is the 0-based position of
    the part among every attachment-like part of the email (in MIME tree
    order, skipped ones included), so it is stable across re-parses of the
    same bytes and keeps duplicate filenames distinct.
    """

    ordinal: int
    filename: str | None
    content_type: str
    content_id: str | None
    payload: bytes = field(repr=False)

    @property
    def decoded_size(self) -> int:
        return len(self.payload)

    @property
    def display_name(self) -> str:
        return self.filename or f"attachment-{self.ordinal + 1}"


@dataclass(frozen=True, slots=True)
class SkippedAttachment:
    ordinal: int
    filename: str | None
    content_type: str
    size: int | None
    reason: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentExtraction:
    attachments: tuple[AttachmentPart, ...] = ()
    skipped: tuple[SkippedAttachment, ...] = ()

    @property
    def seen(self) -> int:
        return len(self.attachments) + len(self.skipped)


def sanitize_filename(raw: str | None) -> str | None:
    """A display-safe basename for ``raw`` -- directory components (POSIX
    or Windows style), control characters and surrounding whitespace are
    dropped. ``None`` when nothing meaningful is left (``..``, ``""``).
    Never used to build a filesystem path.
    """
    if raw is None:
        return None
    text = str(raw)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = PureWindowsPath(PurePosixPath(text.replace("\\", "/")).name).name.strip()
    if text in ("", ".", ".."):
        return None
    return text[:255]


def _safe_filename(part: Message) -> str | None:
    try:
        return sanitize_filename(part.get_filename())
    except Exception:  # malformed RFC 2231 params -- treat as unnamed
        return None


def _content_id(part: Message) -> str | None:
    value = part.get("Content-ID")
    if value is None:
        return None
    return str(value).strip().strip("<>").strip() or None


def _is_attachment(part: Message, body_parts: set[int]) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    if id(part) in body_parts:
        return False
    return _safe_filename(part) is not None


def _body_part_ids(message: EmailMessage) -> set[int]:
    """Identity of the leaf part(s) ``policy.default`` would select as the
    email's readable body -- the same text Docling's EMAIL backend indexes
    -- so a filename-bearing body part is not double-counted as an
    attachment.
    """
    ids: set[int] = set()
    for preference in (("plain",), ("html",)):
        try:
            body = message.get_body(preferencelist=preference)
        except Exception:
            body = None
        if body is not None and (body.get_content_disposition() or "") != "attachment":
            ids.add(id(body))
    return ids


def _iter_leaves(part: Message) -> list[Message]:
    """MIME tree leaves in document order. ``message/rfc822`` parts are
    returned as leaves (never descended into -- V1 does not recurse into
    attached emails).
    """
    if part.get_content_type() == "message/rfc822":
        return [part]
    if part.is_multipart():
        out: list[Message] = []
        for child in part.get_payload():
            if isinstance(child, Message):
                out.extend(_iter_leaves(child))
        return out
    return [part]


def parse_message(raw: bytes) -> EmailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(message, EmailMessage)
    return message


def extract_attachments(
    source: bytes | Path, settings: EmailAttachmentSettings | None = None
) -> AttachmentExtraction:
    """Enumerate ``source``'s attachments, applying ``settings``' limits.

    Deterministic: the same bytes always yield the same ordinals, order
    and skip decisions. A plain email with no attachments yields an empty
    result.
    """
    settings = settings or EmailAttachmentSettings()
    raw = source.read_bytes() if isinstance(source, Path) else source
    message = parse_message(raw)
    body_parts = _body_part_ids(message)

    attachments: list[AttachmentPart] = []
    skipped: list[SkippedAttachment] = []
    total_bytes = 0
    ordinal = 0
    leaves = [] if not message.is_multipart() else _iter_leaves(message)
    for part in leaves:
        content_type = part.get_content_type()
        is_nested = content_type == "message/rfc822"
        if not is_nested and not _is_attachment(part, body_parts):
            continue
        current = ordinal
        ordinal += 1
        filename = _safe_filename(part)

        def _skip(
            reason: str,
            size: int | None = None,
            detail: str | None = None,
            *,
            _ordinal: int = current,
            _filename: str | None = filename,
            _content_type: str = content_type,
        ) -> None:
            skipped.append(
                SkippedAttachment(_ordinal, _filename, _content_type, size, reason, detail)
            )

        if is_nested:
            _skip(SKIP_NESTED_MESSAGE)
            continue
        if len(attachments) >= settings.max_count:
            _skip(SKIP_COUNT_LIMIT)
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception as exc:  # pragma: no cover - stdlib is lenient
            _skip(SKIP_DECODE_ERROR, detail=type(exc).__name__)
            continue
        if payload is None:
            _skip(SKIP_DECODE_ERROR, detail="no decodable payload")
            continue
        if not isinstance(payload, bytes):  # pragma: no cover - defensive
            payload = bytes(payload)
        size = len(payload)
        if size == 0:
            _skip(SKIP_EMPTY, size=0)
            continue
        if size > settings.max_bytes:
            _skip(SKIP_TOO_LARGE, size=size)
            continue
        if total_bytes + size > settings.total_max_bytes:
            _skip(SKIP_TOTAL_LIMIT, size=size)
            continue
        total_bytes += size
        attachments.append(
            AttachmentPart(
                ordinal=current,
                filename=filename,
                content_type=content_type,
                content_id=_content_id(part),
                payload=payload,
            )
        )
    return AttachmentExtraction(attachments=tuple(attachments), skipped=tuple(skipped))
