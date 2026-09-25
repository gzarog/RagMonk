"""``create_backend`` -- given a :class:`~ragmonk.core.config.StorageConfig`,
return the right :class:`~ragmonk.backends.base.KnowledgeBackend` instance.

Storage backend abstraction plan, Phase 1: only ``mode="local"`` returns a
real, working backend today (:class:`ragmonk.backends.local.LocalKnowledgeBackend`).
``mode="server"`` raises :class:`NotImplementedError` with a clear message
naming the engine and the future phase that will add it -- there is no
OpenSearch/Elasticsearch adapter yet.

Nothing in this module imports ``opensearch-py``/``elasticsearch-py``, even
lazily, because there is nothing to construct yet. When those adapters
land, their imports must stay inside this module's function bodies (or
behind ``typing.TYPE_CHECKING``), never at module top level, so
constructing a local backend never requires either package to be
installed.
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
        if engine in _CREDENTIAL_ENV_VARS:
            raise NotImplementedError(
                f"storage.mode='server' with engine={engine!r} has no adapter yet -- "
                "OpenSearch/Elasticsearch KnowledgeBackend adapters are a future phase "
                "of the storage backend abstraction plan (not yet implemented)."
            )
        raise ConfigError(f"unknown storage.server.engine {engine!r}")

    raise ConfigError(f"unknown storage.mode {config.mode!r}")
