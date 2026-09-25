"""Elasticsearch client construction and health/version checks.

Storage backend abstraction plan, Phase 5. ``elasticsearch`` (the
official Python client) is an OPTIONAL dependency
(``pip install "ragmonk[elasticsearch]"``) -- nothing in this module
imports it at module scope, only inside function bodies (and behind
``TYPE_CHECKING`` for type hints), so importing
``ragmonk.backends.elasticsearch_client`` -- and by extension
``ragmonk.backends.elasticsearch`` and ``ragmonk.backends.factory`` for
local-mode construction -- never requires it to be installed.

Credentials are read *only* from environment variables at call time,
never from ``ServerStorageConfig`` (which deliberately holds no
credential field -- see that class's docstring) and never logged. Every
exception raised here is a typed :class:`ElasticsearchConnectionError`;
its message never includes a credential value.

Version compatibility: this adapter targets Elasticsearch's modern
``dense_vector``/``knn`` query surface, which requires at least
Elasticsearch 8.0 (the 7.x line's ``dense_vector`` fields don't support
native ``knn`` queries the way this adapter's ``semantic_search`` relies
on). A cluster reporting a version below that floor is rejected with a
clear, typed :class:`ElasticsearchVersionError` rather than silently
degrading in some partially-working way.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ragmonk.core.errors import HealthCheckError

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from elasticsearch import Elasticsearch

_MIN_SUPPORTED_MAJOR = 8


class ElasticsearchConnectionError(HealthCheckError):
    """Raised when the Elasticsearch client cannot be constructed, or a
    health/info call against a configured cluster fails. Never carries a
    credential value in its message.
    """


class ElasticsearchDependencyError(ElasticsearchConnectionError):
    """Raised when ``elasticsearch`` (the Python client) is not installed
    but a server backend with ``engine="elasticsearch"`` was constructed
    anyway.
    """


class ElasticsearchVersionError(ElasticsearchConnectionError):
    """Raised when a reachable cluster reports a version below this
    adapter's supported floor (Elasticsearch 8.0 -- see module
    docstring).
    """


def _import_elasticsearch() -> Any:
    try:
        import elasticsearch
    except ImportError as exc:
        raise ElasticsearchDependencyError(
            "elasticsearch (the Python client) is not installed. Install it with "
            "'pip install \"ragmonk[elasticsearch]\"' to use "
            "storage.server.engine='elasticsearch'."
        ) from exc
    return elasticsearch


def read_credentials(
    username_var: str, password_var: str, api_key_var: str
) -> tuple[tuple[str, str] | None, str | None]:
    """Read Basic-auth (username, password) and/or an API key from the
    named environment variables. Returns ``(basic_auth, api_key)``;
    either may be ``None``. Never returns/logs the raw values anywhere
    but here.
    """
    username = os.environ.get(username_var)
    password = os.environ.get(password_var)
    api_key = os.environ.get(api_key_var)
    basic_auth = (username, password) if username and password else None
    return basic_auth, api_key


def build_client(
    *,
    url: str,
    verify_tls: bool,
    request_timeout_seconds: float,
    username_var: str,
    password_var: str,
    api_key_var: str,
) -> Elasticsearch:
    """Construct a real ``elasticsearch.Elasticsearch`` client. Raises
    :class:`ElasticsearchDependencyError` if the ``elasticsearch``
    package is not installed, or :class:`ElasticsearchConnectionError`
    for a malformed ``url``.
    """
    es_module = _import_elasticsearch()
    if not url:
        raise ElasticsearchConnectionError(
            "storage.server.url is empty; an Elasticsearch backend needs a cluster URL"
        )

    basic_auth, api_key = read_credentials(username_var, password_var, api_key_var)
    kwargs: dict[str, Any] = {
        "hosts": [url],
        "verify_certs": verify_tls,
        "request_timeout": request_timeout_seconds,
    }
    if basic_auth is not None:
        kwargs["basic_auth"] = basic_auth
    elif api_key is not None:
        # elasticsearch-py accepts either a base64 "id:api_key" string or
        # an (id, api_key) tuple for `api_key`; a single env var is
        # treated as an already-encoded value.
        kwargs["api_key"] = api_key

    try:
        return es_module.Elasticsearch(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive, client rarely raises here
        raise ElasticsearchConnectionError(
            f"failed to construct Elasticsearch client for configured cluster: "
            f"{type(exc).__name__}"
        ) from exc


def cluster_health(client: Elasticsearch) -> tuple[str, str]:
    """Perform a real ``info``/cluster-health call. Returns
    ``(distribution_name, version)``. Raises
    :class:`ElasticsearchConnectionError` on any failure -- network
    error, auth failure, timeout -- with no credential value in the
    message. Raises :class:`ElasticsearchVersionError` when the cluster
    is reachable but reports a version below this adapter's supported
    floor.
    """
    try:
        info = client.info()
    except Exception as exc:
        raise ElasticsearchConnectionError(
            f"Elasticsearch cluster is unreachable or rejected the request: {type(exc).__name__}"
        ) from exc

    info_dict = dict(info) if not isinstance(info, dict) else info
    version_block = info_dict.get("version", {}) if isinstance(info_dict, dict) else {}
    name = str(version_block.get("build_flavor") or info_dict.get("cluster_name") or "elasticsearch")
    version = str(version_block.get("number", "unknown"))
    _validate_version(version)

    try:
        status = client.cluster.health()
    except Exception as exc:
        raise ElasticsearchConnectionError(
            f"Elasticsearch cluster health check failed: {type(exc).__name__}"
        ) from exc
    status_dict = dict(status) if not isinstance(status, dict) else status
    if "status" not in status_dict:
        raise ElasticsearchConnectionError("Elasticsearch cluster health response was malformed")
    return name, version


def _validate_version(version: str) -> None:
    """Rejects any reported version below ``_MIN_SUPPORTED_MAJOR`` (8).
    An unparseable version string is treated as unsupported too, rather
    than silently assumed compatible.
    """
    major_str = version.split(".", 1)[0]
    try:
        major = int(major_str)
    except ValueError:
        raise ElasticsearchVersionError(
            f"could not parse Elasticsearch cluster version {version!r}; "
            f"this adapter requires Elasticsearch {_MIN_SUPPORTED_MAJOR}.0 or newer"
        ) from None
    if major < _MIN_SUPPORTED_MAJOR:
        raise ElasticsearchVersionError(
            f"Elasticsearch cluster reports version {version}, which is below this adapter's "
            f"supported floor of {_MIN_SUPPORTED_MAJOR}.0 (native `dense_vector`/`knn` query "
            "support). Upgrade the cluster, or use storage.server.engine='opensearch' instead."
        )
