"""``cli/_common.py``: the shared ``--json`` envelope/printer every CLI
command with a ``--json`` flag goes through.
"""

from __future__ import annotations

import json

import pytest

from ragpilot.cli._common import json_envelope, print_json


def test_json_envelope_shape() -> None:
    envelope = json_envelope({"query": "ldl"})
    assert envelope == {"schema_version": "1", "data": {"query": "ldl"}}


def test_print_json_does_not_escape_non_ascii_content(capsys: pytest.CaptureFixture[str]) -> None:
    # A real bug hit against a live install: Greek (and any other
    # non-Latin) text extracted from an indexed PDF came out as
    # unreadable \uXXXX escapes in `ragpilot search --json` output,
    # because json.dumps defaults to ensure_ascii=True.
    print_json({"title": "Ακριβές Αντίγραφο", "snippet": "Ουρία (Urea) ... 17.0 mg/dL"})

    captured = capsys.readouterr().out
    assert "Ακριβές Αντίγραφο" in captured
    assert "Ουρία" in captured
    assert "\\u" not in captured

    payload = json.loads(captured)
    assert payload["data"]["title"] == "Ακριβές Αντίγραφο"
