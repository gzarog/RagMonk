"""Search screen (§8)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_search_empty_state(indexed_client: TestClient) -> None:
    response = indexed_client.get("/search")
    assert response.status_code == 200
    assert "Query" in response.text


def test_lexical_search_returns_hits(indexed_client: TestClient) -> None:
    response = indexed_client.get("/search", params={"q": "foo", "mode": "lexical"})
    assert response.status_code == 200
    assert "Lexical results" in response.text


def test_semantic_disabled_shows_reason(indexed_client: TestClient) -> None:
    # search.semantic defaults to off, so semantic mode must degrade with a
    # reason rather than error (§8.1 graceful degradation).
    response = indexed_client.get("/search", params={"q": "foo", "mode": "semantic"})
    assert response.status_code == 200
    assert "disabled" in response.text
