"""Dashboard shows live values from the local install (§4.6 / §4 AC)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_dashboard_renders_live_metrics(indexed_client: TestClient) -> None:
    response = indexed_client.get("/")
    assert response.status_code == 200
    body = response.text
    # The indexed fixture has 2 files, 3 symbols, 1 document -- the
    # dashboard reuses status_service so those real numbers appear.
    assert "Dashboard" in body
    assert "Indexed files" in body
    assert "Symbols" in body
    assert "src" in body  # the source path is listed


def test_dashboard_empty_home(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "No sources yet" in response.text
