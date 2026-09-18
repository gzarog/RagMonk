"""App creation, health probe, static files and clean shutdown (§4 AC)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.ui.conftest import BASE_URL

from ragmonk.ui.app import create_app


def test_app_factory_builds_distinct_apps(ragmonk_home: Path) -> None:
    app_a = create_app(home=ragmonk_home)
    app_b = create_app(home=ragmonk_home)
    assert app_a is not app_b


def test_health_probe(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_htmx_is_served(client: TestClient) -> None:
    response = client.get("/static/js/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text.lower()


def test_static_css_is_served(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert "RagMonk" in response.text


def test_lifespan_bootstraps_and_closes_context(ragmonk_home: Path) -> None:
    application = create_app(home=ragmonk_home)
    with TestClient(application, base_url=BASE_URL) as client:
        assert client.get("/health").status_code == 200
        assert application.state.ctx is not None
    # After the context manager exits, the shared context has been closed
    # (its sources connection is unusable) -- proves the shutdown path ran.
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.ProgrammingError):
        application.state.ctx.sources_conn.execute("SELECT 1")
