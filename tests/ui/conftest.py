"""Fixtures for the admin UI tests.

Builds a real RagMonk home (init + one indexed source) and wraps the
FastAPI app in a ``TestClient`` bound to the allowed localhost host so the
host-validation middleware admits requests. A helper returns the CSRF
token so mutating-request tests can pass the double-submit header.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ragmonk.cli.main import app as cli_app
from ragmonk.ui.app import create_app

BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def indexed_home(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A RagMonk home with one small source indexed (code + a document)."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def foo():\n    return bar()\n\ndef bar():\n    return 1\n")
    (source_dir / "readme.md").write_text("# Title\n\nSome text about foo().\n")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(cli_app, ["init"]).exit_code == 0
    assert runner.invoke(cli_app, ["source", "add", str(source_dir)]).exit_code == 0
    assert runner.invoke(cli_app, ["index"]).exit_code == 0
    return ragmonk_home


@pytest.fixture
def client(ragmonk_home: Path) -> Iterator[TestClient]:
    """A TestClient over a fresh app bound to this test's home."""
    application = create_app(home=ragmonk_home)
    with TestClient(application, base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture
def indexed_client(indexed_home: Path) -> Iterator[TestClient]:
    application = create_app(home=indexed_home)
    with TestClient(application, base_url=BASE_URL) as test_client:
        yield test_client


def csrf_headers(test_client: TestClient) -> dict[str, str]:
    """Prime and return the CSRF double-submit header for unsafe requests."""
    test_client.get("/")
    token = test_client.cookies.get("ragmonk_csrf")
    assert token is not None
    return {"X-CSRF-Token": token}
