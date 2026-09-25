"""``cli/_common.py``: the shared ``--json`` envelope/printer every CLI
command with a ``--json`` flag goes through.
"""

from __future__ import annotations

import json

import pytest
import typer

from ragmonk.cli._common import cli_command, json_envelope, print_json
from ragmonk.core.errors import ConfigError


def test_json_envelope_shape() -> None:
    envelope = json_envelope({"query": "ldl"})
    assert envelope == {"schema_version": "1", "data": {"query": "ldl"}}


def test_print_json_does_not_escape_non_ascii_content(capsys: pytest.CaptureFixture[str]) -> None:
    # A real bug hit against a live install: Greek (and any other
    # non-Latin) text extracted from an indexed PDF came out as
    # unreadable \uXXXX escapes in `ragmonk search --json` output,
    # because json.dumps defaults to ensure_ascii=True.
    print_json({"title": "Ακριβές Αντίγραφο", "snippet": "Ουρία (Urea) ... 17.0 mg/dL"})

    captured = capsys.readouterr().out
    assert "Ακριβές Αντίγραφο" in captured
    assert "Ουρία" in captured
    assert "\\u" not in captured

    payload = json.loads(captured)
    assert payload["data"]["title"] == "Ακριβές Αντίγραφο"


# -- cli_command boundary: never leak a credential to the terminal --------


def test_cli_command_scrubs_embedded_url_credential_from_ragmonk_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Independent review follow-up (credential leak fuzzing): a
    third-party HTTP client exception can carry a URL with embedded
    ``user:pass@`` userinfo inside its own message (this codebase's own
    exceptions never build one, but nothing stops a wrapped/underlying
    exception's ``str()`` from including one -- see
    ``redact_urls_in_text``'s docstring). The CLI boundary must scrub it
    even when raised as a typed ``RagMonkError``.
    """

    @cli_command
    def _boom() -> None:
        raise ConfigError(
            "OpenSearch cluster is unreachable: "
            "ConnectionError(<urllib3.HTTPSConnectionPool(host='opensearch', port=9200)>: "
            "Max retries exceeded with url: /_bulk (Caused by "
            "NewConnectionError('https://admin:s3cr3t-pass@opensearch:9200/_bulk')))"
        )

    with pytest.raises(typer.Exit):
        _boom()

    captured = capsys.readouterr().err
    assert "s3cr3t-pass" not in captured
    assert "admin:" not in captured
    assert "opensearch:9200/_bulk" in captured  # host/path still shown, just not the userinfo


def test_cli_command_scrubs_embedded_url_credential_from_generic_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @cli_command
    def _boom() -> None:
        raise RuntimeError("failed talking to https://user:hunter2@es.example.com:9243/_search")

    with pytest.raises(typer.Exit):
        _boom()

    captured = capsys.readouterr().err
    assert "hunter2" not in captured
    assert "user:" not in captured
    assert "es.example.com:9243/_search" in captured
