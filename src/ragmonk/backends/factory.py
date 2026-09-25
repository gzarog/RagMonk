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

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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


_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@]+@")


def redact_urls_in_text(text: str) -> str:
    """Defensive backstop for the CLI's top-level exception boundary
    (``cli/_common.py``'s ``cli_command`` wrapper) and any other place a
    raw, unstructured message might get printed -- unlike
    :func:`redact_url`, which strips userinfo from one already-parsed
    URL, this scrubs *every* URL-shaped substring found anywhere inside
    an arbitrary string.

    Independent review follow-up: most of this codebase's own
    server-adapter code never puts a credential in a URL (see
    ``ServerStorageConfig``'s docstring -- credentials are env-var-only)
    and always logs failures via ``type(exc).__name__`` rather than
    ``str(exc)`` (``opensearch_client.py``/``elasticsearch_client.py``).
    But an HTTP client library's own exception ``__str__`` is outside
    this codebase's control (``opensearchpy.ConnectionError.__str__``,
    for one, echoes its wrapped ``urllib3`` exception's message
    verbatim, which does include the request URL) and could reach the
    CLI's catch-all unwrapped from a call site that -- unlike
    ``cluster_health``/``build_client`` -- does not itself pre-sanitize
    (``client.bulk``/``client.search``/etc. inside the write/read
    paths). If a user ever puts ``user:pass@`` in ``storage.server.url``
    despite the docs saying not to (the exact scenario ``redact_url``
    itself guards against), that is the one way a credential could ride
    along inside such an exception's message -- this closes that same
    gap at the last point before anything reaches the terminal. Never
    raises.
    """
    if not text:
        return text
    try:
        text = _URL_USERINFO_RE.sub(lambda m: m.group(1), text)
        # Completion plan F7: also a scheme-less ``user:password@host``
        # and the literal value of any configured credential env var.
        text = _BARE_USERINFO_RE.sub("", text)
        return _redact_credential_env_values(text)
    except Exception:  # pragma: no cover - defensive, regex sub on str never raises
        return text


_BARE_USERINFO_RE = re.compile(r"(?<![\w/@])[^\s/@:]+:[^\s/@]+@(?=[\w.\-\[])")


def _redact_credential_env_values(text: str) -> str:
    import os

    for names in _CREDENTIAL_ENV_VARS.values():
        for name in names:
            value = os.environ.get(name)
            if value and len(value) >= 4 and value in text:
                text = text.replace(value, "***")
    return text


def redact_url(url: str) -> str:
    """Strips any embedded userinfo (``user:pass@``) from ``url`` before
    it is ever printed/logged. ``ServerStorageConfig.url`` should never
    itself carry credentials (see that class's docstring -- they're read
    from env vars only, via :func:`credential_env_vars`), but this is a
    defensive backstop against someone putting one there anyway, shared
    by every diagnostic/log call site (``ragmonk doctor``'s server
    section, the daemon's server-mode startup check) so none of them can
    forget it. Never raises: an unparsable ``url`` degrades to a
    best-effort ``@``-split rather than surfacing an exception here.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rsplit("@", 1)[-1] if "@" in url else url
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


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
