"""OpenSearch client construction and health checks.

Storage backend abstraction plan, Phase 4. ``opensearch-py`` is an
OPTIONAL dependency (``pip install "ragmonk[opensearch]"``) -- nothing in
this module imports it at module scope, only inside function bodies (and
behind ``TYPE_CHECKING`` for type hints), so importing
``ragmonk.backends.opensearch_client`` -- and by extension
``ragmonk.backends.opensearch`` and ``ragmonk.backends.factory`` for
local-mode construction -- never requires it to be installed.

Credentials are read *only* from environment variables at call time,
never from ``ServerStorageConfig`` (which deliberately holds no
credential field -- see that class's docstring) and never logged. Every
exception raised here is a typed :class:`OpenSearchConnectionError`; its
message never includes a credential value.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ragmonk.core.errors import HealthCheckError

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from opensearchpy import OpenSearch


class OpenSearchConnectionError(HealthCheckError):
    """Raised when the OpenSearch client cannot be constructed, or a
    health/info call against a configured cluster fails. Never carries a
    credential value in its message.
    """


class OpenSearchDependencyError(OpenSearchConnectionError):
    """Raised when ``opensearch-py`` is not installed but a server
    backend with ``engine="opensearch"`` was constructed anyway.
    """


def _import_opensearch() -> Any:
    try:
        import opensearchpy
    except ImportError as exc:
        raise OpenSearchDependencyError(
            "opensearch-py is not installed. Install it with "
            "'pip install \"ragmonk[opensearch]\"' to use storage.server.engine='opensearch'."
        ) from exc
    return opensearchpy


def read_credentials(
    username_var: str, password_var: str, api_key_var: str
) -> tuple[tuple[str, str] | None, str | None]:
    """Read Basic-auth (username, password) and/or an API key from the
    named environment variables. Returns ``(http_auth, api_key)``; either
    may be ``None``. Never returns/logs the raw values anywhere but here.
    """
    username = os.environ.get(username_var)
    password = os.environ.get(password_var)
    api_key = os.environ.get(api_key_var)
    http_auth = (username, password) if username and password else None
    return http_auth, api_key


def build_client(
    *,
    url: str,
    verify_tls: bool,
    request_timeout_seconds: float,
    username_var: str,
    password_var: str,
    api_key_var: str,
) -> OpenSearch:
    """Construct a real ``opensearchpy.OpenSearch`` client. Raises
    :class:`OpenSearchDependencyError` if ``opensearch-py`` is not
    installed, or :class:`OpenSearchConnectionError` for a malformed
    ``url``.
    """
    opensearchpy = _import_opensearch()
    if not url:
        raise OpenSearchConnectionError(
            "storage.server.url is empty; an OpenSearch backend needs a cluster URL"
        )

    http_auth, api_key = read_credentials(username_var, password_var, api_key_var)
    kwargs: dict[str, Any] = {
        "hosts": [url],
        "use_ssl": url.startswith("https://"),
        "verify_certs": verify_tls,
        "timeout": request_timeout_seconds,
    }
    if http_auth is not None:
        kwargs["http_auth"] = http_auth
    elif api_key is not None:
        # opensearch-py expects an (id, api_key) tuple or a base64 string;
        # a single env var is treated as an already-encoded API key value.
        kwargs["api_key"] = api_key

    try:
        return opensearchpy.OpenSearch(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive, opensearch-py rarely raises here
        raise OpenSearchConnectionError(
            f"failed to construct OpenSearch client for configured cluster: {type(exc).__name__}"
        ) from exc


def cluster_health(client: OpenSearch) -> tuple[str, str]:
    """Perform a real ``info``/cluster-health call. Returns
    ``(distribution_name, version)``. Raises
    :class:`OpenSearchConnectionError` on any failure -- network error,
    auth failure, timeout -- with no credential value in the message.
    """
    try:
        info = client.info()
    except Exception as exc:
        raise OpenSearchConnectionError(
            f"OpenSearch cluster is unreachable or rejected the request: {type(exc).__name__}"
        ) from exc

    version_block = info.get("version", {}) if isinstance(info, dict) else {}
    name = str(version_block.get("distribution") or info.get("cluster_name") or "opensearch")
    version = str(version_block.get("number", "unknown"))
    try:
        status = client.cluster.health()
    except Exception as exc:
        raise OpenSearchConnectionError(
            f"OpenSearch cluster health check failed: {type(exc).__name__}"
        ) from exc
    if not isinstance(status, dict) or "status" not in status:
        raise OpenSearchConnectionError("OpenSearch cluster health response was malformed")
    return name, version
