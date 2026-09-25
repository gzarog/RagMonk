"""``create_backend`` -- given a :class:`~ragmonk.core.config.StorageConfig`,
return the right :class:`~ragmonk.backends.base.KnowledgeBackend` instance.

Storage backend abstraction plan: ``mode="local"`` returns
:class:`ragmonk.backends.local.LocalKnowledgeBackend` (Phase 1).
``mode="server"`` with ``server.engine="opensearch"`` returns a real,
working :class:`ragmonk.backends.opensearch.OpenSearchKnowledgeBackend`
(Phase 4). ``server.engine="elasticsearch"`` returns a real, working
:class:`ragmonk.backends.elasticsearch.ElasticsearchKnowledgeBackend`
(Phase 5).

Nothing in this module imports ``opensearch-py``/``elasticsearch`` at
module scope -- each server branch's import is local to
``create_backend``'s own function body, and
``ragmonk.backends.opensearch``/``ragmonk.backends.elasticsearch``
themselves only import their respective client packages lazily inside
their methods (see those modules' docstrings), so constructing a local
backend, or importing this module at all, never requires either package
to be installed.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.core.config import StorageConfig
from ragmonk.core.errors import ConfigError

# Env vars credentials are read from at call time, per engine. Never
# written to config.yaml/.ragmonk.yaml, never logged.
_CREDENTIAL_ENV_VARS: dict[str, tuple[str, str, str]] = {
    "opensearch": (
        "RAGMONK_OPENSEARCH_USERNAME",
        "RAGMONK_OPENSEARCH_PASSWORD",
        "RAGMONK_OPENSEARCH_API_KEY",
    ),
    "elasticsearch": (
        "RAGMONK_ELASTICSEARCH_USERNAME",
        "RAGMONK_ELASTICSEARCH_PASSWORD",
        "RAGMONK_ELASTICSEARCH_API_KEY",
    ),
}


def credential_env_vars(engine: str) -> tuple[str, str, str]:
    """Return the (username, password, api_key) env var names for
    ``engine``. Raises :class:`ConfigError` for an unknown engine.
    """
    try:
        return _CREDENTIAL_ENV_VARS[engine]
    except KeyError as exc:
        raise ConfigError(f"unknown storage server engine {engine!r}") from exc


def create_backend(config: StorageConfig, *, home: Path | None = None) -> KnowledgeBackend:
    """Construct the ``KnowledgeBackend`` selected by ``config``.

    ``home`` is only used for ``mode="local"`` (the runtime directory the
    local SQLite stack lives under); it is ignored for server modes.
    """
    if config.mode == "local":
        # Local import: keeps this factory importable (and local-mode
        # construction working) without pulling in anything beyond the
        # existing local stack, and mirrors the lazy-import discipline the
        # server branch below will need once real adapters exist.
        from ragmonk.backends.local import LocalKnowledgeBackend

        return LocalKnowledgeBackend(home=home)

    if config.mode == "server":
        engine = config.server.engine
        if engine == "opensearch":
            # Local import: OpenSearchKnowledgeBackend only imports
            # opensearch-py lazily too (inside its own methods), but
            # keeping the import here as well means this factory module
            # never needs opensearch-py installed unless server mode with
            # engine="opensearch" is actually selected.
            from ragmonk.backends.opensearch import OpenSearchKnowledgeBackend

            return OpenSearchKnowledgeBackend(config.server)
        if engine == "elasticsearch":
            # Local import: ElasticsearchKnowledgeBackend only imports
            # the `elasticsearch` package lazily too (inside its own
            # methods), but keeping the import here as well means this
            # factory module never needs it installed unless server mode
            # with engine="elasticsearch" is actually selected.
            from ragmonk.backends.elasticsearch import ElasticsearchKnowledgeBackend

            return ElasticsearchKnowledgeBackend(config.server)
        raise ConfigError(f"unknown storage.server.engine {engine!r}")

    raise ConfigError(f"unknown storage.mode {config.mode!r}")
