"""Source administration via the UI (§5 acceptance criteria)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.ui.conftest import csrf_headers


def test_add_enable_disable_remove_source(client: TestClient, tmp_path: Path) -> None:
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    (source_dir / "note.md").write_text("# hi\n")
    headers = csrf_headers(client)

    # Add
    add = client.post("/sources", data={"path": str(source_dir)}, headers=headers)
    assert add.status_code == 204
    assert add.headers["HX-Redirect"] == "/sources"

    listing = client.get("/sources")
    assert str(source_dir) in listing.text

    # Find the real source id from the detail link in the listing.
    import re

    match = re.search(r'href="/sources/([^"/]+)"', listing.text)
    assert match, listing.text
    sid = match.group(1)

    # Disable
    disable = client.post(f"/sources/{sid}/disable", headers=headers)
    assert disable.status_code == 204
    assert "disabled" in client.get("/sources").text

    # Enable
    client.post(f"/sources/{sid}/enable", headers=headers)
    assert "enabled" in client.get("/sources").text

    # Detail page renders
    detail = client.get(f"/sources/{sid}")
    assert detail.status_code == 200
    assert str(source_dir) in detail.text

    # Remove
    remove = client.post(f"/sources/{sid}/remove", headers=headers)
    assert remove.status_code == 204
    assert str(source_dir) not in client.get("/sources").text


def test_add_nonexistent_source_shows_error(client: TestClient) -> None:
    headers = csrf_headers(client)
    response = client.post("/sources", data={"path": "/no/such/dir"}, headers=headers)
    # Redirect carries an error message rather than 500ing.
    assert response.status_code == 204
    assert "error" in response.headers["HX-Redirect"]
