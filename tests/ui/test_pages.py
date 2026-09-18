"""Smoke coverage for the remaining admin pages (§8/§10/§11/§12/§13)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "path",
    ["/knowledge", "/ai", "/daemon", "/health/", "/backups", "/logs", "/system"],
)
def test_page_renders(indexed_client: TestClient, path: str) -> None:
    assert indexed_client.get(path).status_code == 200


def test_knowledge_lists_symbols(indexed_client: TestClient) -> None:
    body = indexed_client.get("/knowledge").text
    # The fixture defines foo()/bar(); at least one symbol should be listed.
    assert "foo" in body or "bar" in body


def test_ai_page_shows_provider_state(indexed_client: TestClient) -> None:
    body = indexed_client.get("/ai").text
    assert "Selected provider" in body
    assert "never displayed" in body  # the "credentials never displayed" note


def test_daemon_page_shows_stopped(indexed_client: TestClient) -> None:
    assert "STOPPED" in indexed_client.get("/daemon").text


def test_health_page_shows_overall(indexed_client: TestClient) -> None:
    body = indexed_client.get("/health/").text
    assert "HEALTHY" in body or "UNHEALTHY" in body


def test_backups_page_shows_update_status(indexed_client: TestClient) -> None:
    assert "Installed version" in indexed_client.get("/backups").text


def test_logs_page_reads_log_file(indexed_client: TestClient) -> None:
    assert "ragmonk.log" in indexed_client.get("/logs").text
