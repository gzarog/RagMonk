"""Storage backend abstraction plan, Phase 1: ``ragmonk.backends`` factory
and local-backend contract tests. Doubles as the shared fixture future
OpenSearch/Elasticsearch backend-contract tests can extend (see
``LOCAL_BACKEND_METHODS`` and ``local_backend`` fixture).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.factory import create_backend, credential_env_vars
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


def test_factory_server_mode_raises_not_implemented() -> None:
    config = StorageConfig(mode="server")
    with pytest.raises(NotImplementedError, match="opensearch"):
        create_backend(config)


def test_factory_server_mode_elasticsearch_raises_not_implemented() -> None:
    config = StorageConfig(mode="server", server=ServerStorageConfig(engine="elasticsearch"))
    with pytest.raises(NotImplementedError, match="elasticsearch"):
        create_backend(config)


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
    for module_name in ("opensearchpy", "elasticsearch"):
        assert importlib.util.find_spec(module_name) is None, (
            f"{module_name} is installed in this test environment; this test can no "
            "longer prove local mode doesn't need it. Run it in an environment without "
            "opensearch-py/elasticsearch-py installed."
        )

    config = RagMonkConfig()
    backend = create_backend(config.storage, home=tmp_path / "home")
    try:
        assert backend.health() is True
        backend.ensure_schema()
    finally:
        backend.close()


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
