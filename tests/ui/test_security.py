"""Security controls: CSRF, host validation, no secrets, localhost default (§14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.ui.conftest import BASE_URL, csrf_headers

from ragmonk.ui.app import create_app
from ragmonk.ui.security import _allowed_hosts


def test_csrf_blocks_unsafe_request_without_token(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/sources", data={"path": str(tmp_path)})
    assert response.status_code == 403


def test_csrf_allows_unsafe_request_with_token(client: TestClient, tmp_path: Path) -> None:
    source_dir = tmp_path / "d"
    source_dir.mkdir()
    headers = csrf_headers(client)
    response = client.post("/sources", data={"path": str(source_dir)}, headers=headers)
    assert response.status_code == 204


def test_host_validation_rejects_foreign_host(client: TestClient) -> None:
    response = client.get("/", headers={"Host": "attacker.example.com"})
    assert response.status_code == 421


def test_host_validation_allows_localhost() -> None:
    allowed = _allowed_hosts("127.0.0.1", 8765)
    assert "127.0.0.1:8765" in allowed
    assert "localhost:8765" in allowed


def test_default_bind_is_localhost() -> None:
    # create_app's default host is 127.0.0.1 (§14.1), never 0.0.0.0.
    import inspect

    signature = inspect.signature(create_app)
    assert signature.parameters["host"].default == "127.0.0.1"


def test_api_keys_never_displayed(ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-should-not-appear")
    application = create_app(home=ragmonk_home)
    with TestClient(application, base_url=BASE_URL) as client:
        for path in ("/config", "/ai"):
            assert "sk-secret-value-should-not-appear" not in client.get(path).text


def test_destructive_remove_requires_confirmation_markup(indexed_client: TestClient) -> None:
    # The remove control must carry an hx-confirm prompt (§14.4).
    body = indexed_client.get("/sources").text
    assert "hx-confirm" in body


def test_csrf_cookie_is_set_on_first_visit(client: TestClient) -> None:
    client.get("/")
    assert client.cookies.get("ragmonk_csrf")
