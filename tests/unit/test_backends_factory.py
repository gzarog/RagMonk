"""Storage backend abstraction plan, Phase 1: ``ragmonk.backends`` factory
and local-backend contract tests. Doubles as the shared fixture future
OpenSearch/Elasticsearch backend-contract tests can extend (see
``LOCAL_BACKEND_METHODS`` and ``local_backend`` fixture).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.factory import create_backend, credential_env_vars, redact_urls_in_text
from ragmonk.backends.local import LocalKnowledgeBackend
from ragmonk.core.config import RagMonkConfig, ServerStorageConfig, StorageConfig
from ragmonk.core.errors import ConfigError


@pytest.fixture
def local_backend(tmp_path: Path) -> Iterator[KnowledgeBackend]:
    """Shared fixture: a real LocalKnowledgeBackend against a scratch
    runtime dir. Future backend-contract test modules (OpenSearch,
    Elasticsearch) can mirror this fixture's shape -- construct their own
    backend, yield it, and run the same contract assertions.
    """
    backend = LocalKnowledgeBackend(home=tmp_path / "home")
    yield backend
    backend.close()


def test_factory_local_mode_returns_local_backend(tmp_path: Path) -> None:
    config = StorageConfig(mode="local")
    backend = create_backend(config, home=tmp_path / "home")
    try:
        assert isinstance(backend, LocalKnowledgeBackend)
        assert backend.health() is True
    finally:
        backend.close()


def test_factory_server_mode_opensearch_returns_opensearch_backend() -> None:
    """Storage backend abstraction plan, Phase 4: ``mode="server"`` with
    the (default) ``engine="opensearch"`` now returns a real, working
    ``OpenSearchKnowledgeBackend`` instead of raising. This only needs
    opensearch-py installed to *construct*; it does not connect (no
    method that talks to a cluster is called here).
    """
    pytest.importorskip("opensearchpy")
    from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend

    config = StorageConfig(mode="server")
    backend = create_backend(config)
    assert isinstance(backend, OpenSearchKnowledgeBackend)


def test_factory_server_mode_elasticsearch_returns_elasticsearch_backend() -> None:
    """Storage backend abstraction plan, Phase 5: ``mode="server"`` with
    ``engine="elasticsearch"`` now returns a real, working
    ``ElasticsearchKnowledgeBackend`` instead of raising. This only needs
    the ``elasticsearch`` package installed to *construct*; it does not
    connect (no method that talks to a cluster is called here).
    """
    pytest.importorskip("elasticsearch")
    from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend

    config = StorageConfig(mode="server", server=ServerStorageConfig(engine="elasticsearch"))
    backend = create_backend(config)
    assert isinstance(backend, ElasticsearchKnowledgeBackend)


def test_credential_env_vars_known_engines() -> None:
    assert credential_env_vars("opensearch") == (
        "RAGMONK_OPENSEARCH_USERNAME",
        "RAGMONK_OPENSEARCH_PASSWORD",
        "RAGMONK_OPENSEARCH_API_KEY",
    )
    assert credential_env_vars("elasticsearch") == (
        "RAGMONK_ELASTICSEARCH_USERNAME",
        "RAGMONK_ELASTICSEARCH_PASSWORD",
        "RAGMONK_ELASTICSEARCH_API_KEY",
    )


def test_credential_env_vars_unknown_engine_raises() -> None:
    with pytest.raises(ConfigError):
        credential_env_vars("solr")


def test_local_backend_construction_needs_no_server_sdk(tmp_path: Path) -> None:
    """Asserts the opensearch/elasticsearch client packages are not
    installed in this environment, and that constructing + using a local
    backend still works -- locking the 'local mode needs no server SDK'
    contract from the inside (a real absence, not a mock).
    """
    # opensearch-py is now an OPTIONAL extra (Phase 4) that this repo's own
    # dev/CI environment may have installed to test the OpenSearch adapter
    # itself (see test_backends_opensearch.py) -- so this test can no
    # longer assert its outright *absence* from the interpreter the way it
    # could pre-Phase-4. What it still proves, either way: local-mode
    # construction and use never *imports* either server SDK -- checked via
    # sys.modules, not find_spec, since find_spec only asks whether a
    # package is installed, not whether local-mode code touched it.
    already_imported = {
        name for name in ("opensearchpy", "elasticsearch") if name in sys.modules
    }

    config = RagMonkConfig()
    backend = create_backend(config.storage, home=tmp_path / "home")
    try:
        assert backend.health() is True
        backend.ensure_schema()
    finally:
        backend.close()

    newly_imported = {
        name for name in ("opensearchpy", "elasticsearch") if name in sys.modules
    } - already_imported
    assert not newly_imported, (
        f"local-mode backend construction/use imported {newly_imported}, "
        "which it must never need"
    )


def test_local_backend_health_and_schema(local_backend: KnowledgeBackend) -> None:
    assert local_backend.health() is True
    local_backend.ensure_schema()  # no raise
    assert local_backend.health() is True


def test_local_backend_unwired_methods_raise_not_implemented(
    local_backend: KnowledgeBackend,
) -> None:
    """Documents, rather than hides, which contract methods
    LocalKnowledgeBackend does not yet wire to the existing SQLite stack
    (future phase). A method here that starts working should move to a
    real-behavior test instead of staying in this list.
    """
    with pytest.raises(NotImplementedError):
        local_backend.begin_generation("source-1")
    with pytest.raises(NotImplementedError):
        local_backend.lexical_search("query", 10)
    with pytest.raises(NotImplementedError):
        local_backend.count_stats()


# -- redact_urls_in_text: credential-leak fuzzing follow-up ----------------


def test_redact_urls_in_text_strips_userinfo_from_every_embedded_url() -> None:
    text = (
        "ConnectionError caused by NewConnectionError("
        "'https://admin:s3cr3t@opensearch:9200/_bulk') and also "
        "http://user@es.example.com:9243/_search failed"
    )
    scrubbed = redact_urls_in_text(text)
    assert "s3cr3t" not in scrubbed
    assert "admin:" not in scrubbed
    assert "user@" not in scrubbed
    assert "opensearch:9200/_bulk" in scrubbed
    assert "es.example.com:9243/_search" in scrubbed


def test_redact_urls_in_text_leaves_plain_text_untouched() -> None:
    text = "OpenSearch cluster is unreachable or rejected the request: ConnectionError"
    assert redact_urls_in_text(text) == text


def test_redact_urls_in_text_never_raises_on_empty_or_odd_input() -> None:
    assert redact_urls_in_text("") == ""
    assert redact_urls_in_text("not a url at all @ symbol here") == (
        "not a url at all @ symbol here"
    )
