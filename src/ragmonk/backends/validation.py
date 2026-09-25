"""Real, engine-specific server validation for ``ragmonk init``
(completion plan F5).

Replaces the old generic ``urllib`` HTTP GET preflight, which any HTTP
server answering ``200`` could pass. Validation now goes through the
*selected engine's own client* (``opensearch-py`` or the 8.x
``elasticsearch`` client, built exactly the way the runtime backend
builds it) and checks, before any config is written:

1. the URL carries no credentials (user-info) -- credentials come only
   from the ``RAGMONK_<ENGINE>_*`` env vars;
2. the endpoint is reachable and the credentials are accepted
   (``info()``; 401/403 is reported as an authentication failure);
3. the engine *identity* matches the selection -- an OpenSearch cluster
   must report ``version.distribution == "opensearch"``; an Elasticsearch
   cluster must report the Elasticsearch tagline and a ``build_flavor``
   and must not report an OpenSearch distribution (the 8.x Elasticsearch
   client additionally refuses a server without the
   ``X-Elastic-Product: Elasticsearch`` header). A generic HTTP 200 stub
   passes neither;
4. the version is supported (OpenSearch >= 2.0, Elasticsearch >= 8.0);
5. required permissions/capabilities, via non-destructive calls only:
   cluster health (``cluster:monitor/health``), index-existence checks
   on the three configured indices, Elasticsearch's
   ``security.has_privileges`` for the index privileges the backend
   needs (when security is enabled), and -- as a warning, since only
   semantic search needs it -- OpenSearch's k-NN plugin.

Every error message is built from exception *type names*, status codes
and the redacted URL only -- never ``str(exc)`` (a client exception's
text can echo request URLs/headers) and never a credential value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ragmonk.core.config import ServerStorageConfig, url_has_userinfo
from ragmonk.core.errors import UsageError

MIN_OPENSEARCH_VERSION = (2, 0)
MIN_ELASTICSEARCH_VERSION = (8, 0)
_ES_TAGLINE = "You Know, for Search"
_REQUIRED_INDEX_PRIVILEGES = ["read", "write", "create_index", "delete", "view_index_metadata"]


class ServerValidationError(UsageError):
    """Server validation failed; nothing was written. Never carries a
    credential value.
    """


@dataclass(frozen=True)
class ServerValidationResult:
    engine: str
    version: str
    indices_present: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    meta = getattr(exc, "meta", None)
    value = getattr(meta, "status", None)
    return value if isinstance(value, int) else None


def _describe_failure(exc: BaseException) -> str:
    name = type(exc).__name__
    status = _status_code(exc)
    if name in ("AuthenticationException", "AuthorizationException") or status in (401, 403):
        return (
            f"authentication/authorization failed ({name}"
            + (f", HTTP {status}" if status else "")
            + "); check the RAGMONK_<ENGINE>_USERNAME/_PASSWORD/_API_KEY env vars"
        )
    if name == "UnsupportedProductError":
        return "the server did not identify itself as Elasticsearch (UnsupportedProductError)"
    return f"unreachable or rejected the request ({name}" + (
        f", HTTP {status})" if status else ")"
    )


def _parse_version(version: str) -> tuple[int, int] | None:
    parts = version.split("-", 1)[0].split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    return major, minor


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    body = getattr(value, "body", None)
    if isinstance(body, dict):
        return body
    try:
        return dict(value)
    except Exception:
        return {}


def build_engine_client(config: ServerStorageConfig) -> Any:
    """The real client for ``config.engine``, built exactly as the
    runtime backend builds it (same TLS/timeout/credential handling).
    """
    from ragmonk.backends.factory import credential_env_vars

    username_var, password_var, api_key_var = credential_env_vars(config.engine)
    kwargs: dict[str, Any] = {
        "url": config.url,
        "verify_tls": config.verify_tls,
        "request_timeout_seconds": config.request_timeout_seconds,
        "username_var": username_var,
        "password_var": password_var,
        "api_key_var": api_key_var,
    }
    if config.engine == "opensearch":
        from ragmonk.backends.opensearch_client import build_client as build_os

        return build_os(**kwargs)
    from ragmonk.backends.elasticsearch_client import build_client as build_es

    return build_es(**kwargs)


def validate_server_config(
    config: ServerStorageConfig, *, client: Any | None = None
) -> ServerValidationResult:
    """Run every check listed in the module docstring against the
    configured cluster. ``client`` lets tests inject a fake; production
    callers leave it ``None`` so the real engine client is used.
    Raises :class:`ServerValidationError` on the first failure.
    """
    from ragmonk.backends.factory import redact_url

    engine = config.engine
    shown_url = redact_url(config.url)
    if not config.url:
        raise ServerValidationError("--storage-url is required when --storage-mode=server")
    if url_has_userinfo(config.url):
        raise ServerValidationError(
            "storage URL must not contain credentials (user-info); set "
            f"RAGMONK_{engine.upper()}_USERNAME/_PASSWORD or _API_KEY instead"
        )

    if client is None:
        try:
            client = build_engine_client(config)
        except Exception as exc:
            # The *Dependency/*ConnectionError messages raised by
            # build_client are this codebase's own, credential-free text.
            from ragmonk.core.errors import HealthCheckError

            detail = str(exc) if isinstance(exc, HealthCheckError) else type(exc).__name__
            raise ServerValidationError(
                f"could not create a {engine} client for {shown_url}: {detail}"
            ) from None

    # 1. reachability + auth + identity
    try:
        info = _as_dict(client.info())
    except Exception as exc:
        raise ServerValidationError(
            f"{engine} at {shown_url}: {_describe_failure(exc)}"
        ) from None
    version_block = info.get("version") if isinstance(info.get("version"), dict) else {}
    assert isinstance(version_block, dict)
    distribution = str(version_block.get("distribution") or "").lower()
    number = str(version_block.get("number") or "")
    if not number:
        raise ServerValidationError(
            f"{shown_url} is not an {engine} cluster: its root endpoint reported no version "
            "information (a generic HTTP server or proxy answered instead)"
        )
    if engine == "opensearch":
        if distribution != "opensearch":
            actual = "Elasticsearch" if info.get("tagline") == _ES_TAGLINE else "an unknown server"
            raise ServerValidationError(
                f"{shown_url} is {actual}, not OpenSearch (version.distribution="
                f"{distribution or 'missing'}); use --storage-engine elasticsearch if intended"
            )
        minimum = MIN_OPENSEARCH_VERSION
    else:
        if distribution == "opensearch":
            raise ServerValidationError(
                f"{shown_url} is OpenSearch, not Elasticsearch; use --storage-engine opensearch"
            )
        if info.get("tagline") != _ES_TAGLINE or not version_block.get("build_flavor"):
            raise ServerValidationError(
                f"{shown_url} did not identify itself as Elasticsearch (missing tagline/"
                "build_flavor)"
            )
        minimum = MIN_ELASTICSEARCH_VERSION

    parsed = _parse_version(number)
    if parsed is None or parsed < minimum:
        raise ServerValidationError(
            f"{engine} at {shown_url} reports version {number or 'unknown'}, which is not "
            f"supported; {engine} {minimum[0]}.{minimum[1]} or newer is required"
        )

    # 2. cluster health permission
    try:
        health = _as_dict(client.cluster.health())
    except Exception as exc:
        raise ServerValidationError(
            f"{engine} at {shown_url}: cluster health check failed: {_describe_failure(exc)}"
        ) from None
    if "status" not in health:
        raise ServerValidationError(f"{engine} at {shown_url}: malformed cluster health response")
    warnings: list[str] = []
    if health.get("status") == "red":
        warnings.append("cluster health is RED")

    # 3. non-destructive index permission checks
    prefix = config.index_prefix
    names = [f"{prefix}-files", f"{prefix}-content", f"{prefix}-relationships"]
    present: dict[str, bool] = {}
    for name in names:
        try:
            present[name] = bool(client.indices.exists(index=name))
        except Exception as exc:
            raise ServerValidationError(
                f"{engine} at {shown_url}: cannot inspect index {name!r}: "
                f"{_describe_failure(exc)}"
            ) from None

    if engine == "elasticsearch":
        security = getattr(client, "security", None)
        has_privileges = getattr(security, "has_privileges", None)
        if callable(has_privileges):
            try:
                response = _as_dict(
                    has_privileges(
                        cluster=["monitor"],
                        index=[
                            {"names": [f"{prefix}-*"], "privileges": _REQUIRED_INDEX_PRIVILEGES}
                        ],
                    )
                )
            except Exception as exc:
                warnings.append(
                    f"could not verify index privileges ({type(exc).__name__}); "
                    "security may be disabled"
                )
            else:
                if response.get("has_all_requested") is False:
                    missing = sorted(
                        priv
                        for idx in (response.get("index") or {}).values()
                        for priv, ok in (idx or {}).items()
                        if not ok
                    )
                    raise ServerValidationError(
                        f"elasticsearch at {shown_url}: the configured credentials lack "
                        f"required privileges on '{prefix}-*': {', '.join(missing) or 'unknown'}"
                    )
    else:
        cat = getattr(client, "cat", None)
        plugins_call = getattr(cat, "plugins", None)
        if callable(plugins_call):
            try:
                plugins = plugins_call(format="json")
                components = {
                    str(p.get("component", "")) for p in (plugins or []) if isinstance(p, dict)
                }
                if "opensearch-knn" not in components:
                    warnings.append(
                        "the OpenSearch k-NN plugin was not found; semantic search will not work"
                    )
            except Exception as exc:
                warnings.append(f"could not list OpenSearch plugins ({type(exc).__name__})")

    return ServerValidationResult(
        engine=engine, version=number, indices_present=present, warnings=warnings
    )
