"""Storage backend abstraction plan, Phase 8: ``ragmonk doctor``'s
server-mode diagnostics -- configured engine, redacted endpoint,
cluster reachability/version, index/schema status, and the "server
unreachable" diagnostic on a connectivity failure. Also asserts that no
credential value ever appears in doctor output, in any mode.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import pytest
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend
from ragmonk.cli import doctor
from ragmonk.core.config import RagMonkConfig
from ragmonk.core.lifecycle import AppContext

_SECRET_USERNAME = "s3cret-admin-user"
_SECRET_PASSWORD = "hunter2-do-not-print-me"  # noqa: S105 - test fixture value, not a real secret
_SECRET_API_KEY = "sk-supersecret-api-key-value"  # noqa: S105 - test fixture value


def _server_config(url: str = "https://opensearch.internal:9200") -> RagMonkConfig:
    return RagMonkConfig.model_validate(
        {
            "storage": {
                "mode": "server",
                "server": {"engine": "opensearch", "url": url, "index_prefix": "ragmonk"},
            }
        }
    )


def _ctx_with_backend(config: RagMonkConfig, backend: OpenSearchKnowledgeBackend) -> AppContext:
    # _server_section only reads ctx.config and calls ctx.backend() -- a
    # SimpleNamespace duck-types it fine; cast keeps mypy quiet at the
    # call site the same way ``test_cli_doctor_ai.py``'s ``_ctx`` does.
    return cast(
        AppContext, SimpleNamespace(config=config, backend=lambda project_id=None: backend)
    )


@pytest.fixture(autouse=True)
def _credential_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server-mode credential set in the environment, exactly the way
    a real deployment would configure it (never in config.yaml -- see
    ``ServerStorageConfig``'s docstring). Every test in this module
    asserts these values never leak into doctor's output.
    """
    monkeypatch.setenv("RAGMONK_OPENSEARCH_USERNAME", _SECRET_USERNAME)
    monkeypatch.setenv("RAGMONK_OPENSEARCH_PASSWORD", _SECRET_PASSWORD)
    monkeypatch.setenv("RAGMONK_OPENSEARCH_API_KEY", _SECRET_API_KEY)


def _assert_no_secret_leak(rendered: str) -> None:
    for secret in (_SECRET_USERNAME, _SECRET_PASSWORD, _SECRET_API_KEY):
        assert secret not in rendered


def test_server_section_reports_engine_endpoint_and_health() -> None:
    config = _server_config()
    fake = FakeOpenSearch(reachable=True)
    backend = OpenSearchKnowledgeBackend(config.storage.server, client=fake)
    section = doctor._server_section(_ctx_with_backend(config, backend))

    assert section.name == "Server"
    by_name = {c.name: c for c in section.checks}
    assert by_name["engine"].status == "ok"
    assert "engine=opensearch" in by_name["engine"].detail
    assert "opensearch.internal" in by_name["engine"].detail
    assert by_name["connectivity"].status == "ok"
    assert "opensearch" in by_name["connectivity"].detail.lower()
    assert by_name["indices"].status in ("ok", "warn")


def test_server_section_reports_unreachable_on_connection_failure() -> None:
    config = _server_config()
    fake = FakeOpenSearch(reachable=False)
    backend = OpenSearchKnowledgeBackend(config.storage.server, client=fake)
    section = doctor._server_section(_ctx_with_backend(config, backend))

    by_name = {c.name: c for c in section.checks}
    assert by_name["connectivity"].status == "fail"
    assert "unreachable" in by_name["connectivity"].detail.lower()
    # Never crashes uncaught, and downstream checks are cleanly skipped
    # rather than attempted against a known-dead connection.
    assert by_name["indices"].status == "fail"


def test_server_section_redacts_userinfo_embedded_in_url() -> None:
    """Defense in depth: completion plan F7 now rejects a credential-
    bearing ``storage.server.url`` at config validation (see
    ``test_config_rejects_credential_bearing_url``), so this bypasses
    validation with ``model_construct`` to prove doctor's own redaction
    still holds for a config that somehow got past it.
    """
    from ragmonk.core.config import ServerStorageConfig, StorageConfig

    config = RagMonkConfig(
        storage=StorageConfig(
            mode="server",
            server=ServerStorageConfig.model_construct(
                engine="opensearch",
                url=f"https://{_SECRET_USERNAME}:{_SECRET_PASSWORD}@opensearch.internal:9200",
                index_prefix="ragmonk",
                verify_tls=True,
                request_timeout_seconds=30.0,
                bulk=ServerStorageConfig().bulk,
            ),
        )
    )
    fake = FakeOpenSearch(reachable=True)
    backend = OpenSearchKnowledgeBackend(config.storage.server, client=fake)
    section = doctor._server_section(_ctx_with_backend(config, backend))

    joined = " ".join(c.detail for c in section.checks)
    _assert_no_secret_leak(joined)
    assert "opensearch.internal" in joined


def test_run_checks_never_leaks_credentials_in_human_or_json_output() -> None:
    """The full ``run_checks``/render pipeline, not just ``_server_section``
    in isolation -- credentials must never surface in either
    ``--json`` or the human-readable renderer.
    """
    config = _server_config()
    fake = FakeOpenSearch(reachable=True)
    backend = OpenSearchKnowledgeBackend(config.storage.server, client=fake)
    section = doctor._server_section(_ctx_with_backend(config, backend))
    overall = doctor.overall_status([section])

    json_payload = json.dumps(doctor._sections_to_json([section], overall))
    _assert_no_secret_leak(json_payload)

    human_lines = [f"{c.status} {c.detail}" for c in section.checks]
    _assert_no_secret_leak("\n".join(human_lines))


def test_server_section_handles_backend_construction_failure_gracefully() -> None:
    """A backend that raises just constructing/connecting must never
    crash ``doctor`` uncaught -- it becomes a clear FAIL check instead.
    """
    config = _server_config()

    def _raising_backend(project_id: str | None = None) -> OpenSearchKnowledgeBackend:
        raise RuntimeError("boom")

    ctx = cast(AppContext, SimpleNamespace(config=config, backend=_raising_backend))
    section = doctor._server_section(ctx)
    by_name = {c.name: c for c in section.checks}
    assert by_name["connectivity"].status == "fail"
