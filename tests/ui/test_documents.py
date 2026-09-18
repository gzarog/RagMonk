"""Documents browser and chunk inspection (§7)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def test_documents_list_shows_indexed_document(indexed_client: TestClient) -> None:
    response = indexed_client.get("/documents")
    assert response.status_code == 200
    assert "readme.md" in response.text


def test_document_detail_shows_chunks(indexed_client: TestClient) -> None:
    listing = indexed_client.get("/documents")
    match = re.search(r'/documents/([^/]+)/([^"]+)"', listing.text)
    assert match, listing.text
    source_id, document_id = match.group(1), match.group(2)

    detail = indexed_client.get(f"/documents/{source_id}/{document_id}")
    assert detail.status_code == 200
    assert "Chunks" in detail.text


def test_documents_filter_by_query(indexed_client: TestClient) -> None:
    response = indexed_client.get("/documents", params={"q": "nonexistentxyz"})
    assert response.status_code == 200
    assert "No documents match" in response.text
