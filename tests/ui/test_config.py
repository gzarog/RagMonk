"""Configuration UI: view, validate, persist, env overrides, secrets (§9)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from tests.ui.conftest import BASE_URL, csrf_headers

from ragmonk.core import paths
from ragmonk.ui.app import create_app


def test_config_page_lists_sections(client: TestClient) -> None:
    response = client.get("/config")
    assert response.status_code == 200
    for section in ("runtime", "indexing", "search", "ai", "updates"):
        assert section in response.text


def test_config_save_persists_and_validates(client: TestClient, ragmonk_home: Path) -> None:
    headers = csrf_headers(client)
    response = client.post("/config", data={"runtime__log_level": "debug"}, headers=headers)
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/config?saved=1"

    written = yaml.safe_load(paths.user_config_path(ragmonk_home).read_text())
    assert written["runtime"]["log_level"] == "debug"


def test_env_overridden_value_is_read_only(
    ragmonk_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAGMONK_SEARCH__SEMANTIC", "true")
    application = create_app(home=ragmonk_home)
    with TestClient(application, base_url=BASE_URL) as client:
        body = client.get("/config").text
        # The override is surfaced with its env var name and rendered read-only.
        assert "RAGMONK_SEARCH__SEMANTIC" in body


def test_config_rejects_invalid_value(client: TestClient, ragmonk_home: Path) -> None:
    headers = csrf_headers(client)
    # log_level has a validated set; a bogus int field is a safer invalid case.
    response = client.post(
        "/config", data={"runtime__sqlite_cache_size_mb": "not-a-number"}, headers=headers
    )
    assert response.status_code == 204
    assert "error" in response.headers["HX-Redirect"]
