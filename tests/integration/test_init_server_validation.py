"""Completion plan F5 (real init validation) and F7 (credential-bearing
URLs rejected, no secret in any error).

Two layers:

- Engine-emulating HTTP servers driven through the *real* client
  libraries (``opensearch-py``/``elasticsearch``) exactly as ``ragmonk
  init`` uses them. These skip cleanly when the optional client isn't
  installed (plain local installs / the default CI job).
- ``validate_server_config`` with injected fake clients, which needs no
  optional dependency and always runs.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from ragmonk.backends.validation import ServerValidationError, validate_server_config
from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.config import ServerStorageConfig

_SECRET_USER = "admin-user-s3cret"
_SECRET_PASSWORD = "hunter2-never-print"  # noqa: S105 - test fixture value


@dataclass
class _Profile:
    kind: str  # "opensearch" | "elasticsearch"
    version: str
    require_auth: bool = False
    seen_auth: list[str] = field(default_factory=list)


def _info(profile: _Profile) -> dict[str, Any]:
    if profile.kind == "opensearch":
        return {
            "name": "node",
            "cluster_name": "c",
            "version": {"distribution": "opensearch", "number": profile.version},
            "tagline": "The OpenSearch Project: https://opensearch.org/",
        }
    return {
        "name": "node",
        "cluster_name": "c",
        "version": {"number": profile.version, "build_flavor": "default"},
        "tagline": "You Know, for Search",
    }


def _make_handler(profile: _Profile) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, status: int, body: Any | None) -> None:
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if profile.kind == "elasticsearch":
                self.send_header("X-Elastic-Product", "Elasticsearch")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization")
            if header:
                profile.seen_auth.append(header)
            return not profile.require_auth or bool(header)

        def _route(self) -> None:
            if not self._authorized():
                self._send(401, {"error": "unauthorized", "status": 401})
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, _info(profile))
            elif path == "/_cluster/health":
                self._send(200, {"status": "green", "cluster_name": "c"})
            elif path == "/_cat/plugins":
                self._send(200, [{"component": "opensearch-knn"}])
            elif path.startswith("/_security/user/_has_privileges"):
                self._send(200, {"has_all_requested": True, "index": {}, "cluster": {}})
            else:
                self._send(404, {"error": "not found", "status": 404})

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            self._route()

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
            self._route()

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._route()

        def log_message(self, *args: object) -> None:
            pass

    return _Handler


@pytest.fixture
def engine_server() -> Iterator[Any]:
    servers: list[http.server.HTTPServer] = []

    def _start(profile: _Profile) -> str:
        server = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(profile))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield _start
    for server in servers:
        server.shutdown()


def _init(runner: CliRunner, engine: str, url: str) -> Any:
    return runner.invoke(
        app,
        [
            "init",
            "--storage-mode",
            "server",
            "--storage-engine",
            engine,
            "--storage-url",
            url,
            "--storage-index-prefix",
            "myproj",
        ],
    )


def _client_available(engine: str) -> bool:
    module = "opensearchpy" if engine == "opensearch" else "elasticsearch"
    try:
        __import__(module)
    except ImportError:
        return False
    return True


# -- real client, emulated engines ------------------------------------------


@pytest.mark.parametrize(
    ("engine", "version"), [("opensearch", "2.11.0"), ("elasticsearch", "8.13.0")]
)
def test_init_real_engine_passes_and_writes_config(
    ragmonk_home: Path, runner: CliRunner, engine_server: Any, engine: str, version: str
) -> None:
    if not _client_available(engine):
        pytest.skip(f"{engine} client not installed")
    url = engine_server(_Profile(kind=engine, version=version))
    result = _init(runner, engine, url)
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(paths.user_config_path(ragmonk_home).read_text())
    assert data["storage"]["mode"] == "server"
    assert data["storage"]["server"]["engine"] == engine
    assert data["storage"]["server"]["url"] == url


@pytest.mark.parametrize(
    ("selected", "actual"), [("opensearch", "elasticsearch"), ("elasticsearch", "opensearch")]
)
def test_init_engine_identity_mismatch_is_rejected(
    ragmonk_home: Path, runner: CliRunner, engine_server: Any, selected: str, actual: str
) -> None:
    if not _client_available(selected):
        pytest.skip(f"{selected} client not installed")
    version = "2.11.0" if actual == "opensearch" else "8.13.0"
    url = engine_server(_Profile(kind=actual, version=version))
    result = _init(runner, selected, url)
    assert result.exit_code != 0, result.output
    assert not paths.user_config_path(ragmonk_home).is_file()


@pytest.mark.parametrize(
    ("engine", "version"), [("opensearch", "1.3.9"), ("elasticsearch", "7.17.0")]
)
def test_init_unsupported_version_fails_before_config_is_written(
    ragmonk_home: Path, runner: CliRunner, engine_server: Any, engine: str, version: str
) -> None:
    if not _client_available(engine):
        pytest.skip(f"{engine} client not installed")
    url = engine_server(_Profile(kind=engine, version=version))
    result = _init(runner, engine, url)
    assert result.exit_code != 0, result.output
    assert "not supported" in result.output
    assert not paths.user_config_path(ragmonk_home).is_file()


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_init_auth_failure_reports_without_leaking_secret(
    ragmonk_home: Path,
    runner: CliRunner,
    engine_server: Any,
    engine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _client_available(engine):
        pytest.skip(f"{engine} client not installed")
    version = "2.11.0" if engine == "opensearch" else "8.13.0"
    url = engine_server(_Profile(kind=engine, version=version, require_auth=True))
    result = _init(runner, engine, url)
    assert result.exit_code != 0
    assert "authentication" in result.output.lower()
    assert not paths.user_config_path(ragmonk_home).is_file()

    # With credentials from the supported env vars it passes, and neither
    # the password nor the header value appears anywhere in the output.
    prefix = f"RAGMONK_{engine.upper()}"
    monkeypatch.setenv(f"{prefix}_USERNAME", _SECRET_USER)
    monkeypatch.setenv(f"{prefix}_PASSWORD", _SECRET_PASSWORD)
    result = _init(runner, engine, url)
    assert result.exit_code == 0, result.output
    assert _SECRET_PASSWORD not in result.output and _SECRET_USER not in result.output
    raw = paths.user_config_path(ragmonk_home).read_text()
    assert _SECRET_PASSWORD not in raw and _SECRET_USER not in raw


# -- F7: credential-bearing URL rejected ---------------------------------------


@pytest.mark.parametrize("engine", ["opensearch", "elasticsearch"])
def test_init_rejects_credential_bearing_url_without_echoing_it(
    ragmonk_home: Path, runner: CliRunner, engine: str
) -> None:
    url = f"https://{_SECRET_USER}:{_SECRET_PASSWORD}@search.example:9200"
    result = _init(runner, engine, url)
    assert result.exit_code != 0
    assert "credentials" in result.output
    assert _SECRET_PASSWORD not in result.output and _SECRET_USER not in result.output
    assert not paths.user_config_path(ragmonk_home).is_file()


# -- validate_server_config with injected fakes (no optional deps) -----------


class _FakeClient:
    def __init__(self, info: Any, health: Any = None, *, raise_on_info: Exception | None = None):
        self._info = info
        self._raise = raise_on_info
        outer = self

        class _Cluster:
            def health(self) -> Any:
                return health if health is not None else {"status": "green"}

        class _Indices:
            def exists(self, index: str) -> bool:
                outer.exists_calls.append(index)
                return False

        self.cluster = _Cluster()
        self.indices = _Indices()
        self.exists_calls: list[str] = []

    def info(self) -> Any:
        if self._raise is not None:
            raise self._raise
        return self._info


def _cfg(engine: str, url: str = "https://search.example:9200") -> ServerStorageConfig:
    return ServerStorageConfig(engine=engine, url=url)  # type: ignore[arg-type]


def test_validate_generic_200_body_is_not_an_engine() -> None:
    for engine in ("opensearch", "elasticsearch"):
        with pytest.raises(ServerValidationError, match="not an"):
            validate_server_config(_cfg(engine), client=_FakeClient({}))


def test_validate_opensearch_accepts_real_identity_and_checks_indices() -> None:
    client = _FakeClient({"version": {"distribution": "opensearch", "number": "2.11.0"}})
    result = validate_server_config(_cfg("opensearch"), client=client)
    assert result.version == "2.11.0"
    assert client.exists_calls == ["ragmonk-files", "ragmonk-content", "ragmonk-relationships"]


def test_validate_elasticsearch_must_not_accept_opensearch() -> None:
    client = _FakeClient({"version": {"distribution": "opensearch", "number": "2.11.0"}})
    with pytest.raises(ServerValidationError, match="is OpenSearch"):
        validate_server_config(_cfg("elasticsearch"), client=client)


def test_validate_opensearch_must_not_accept_elasticsearch() -> None:
    client = _FakeClient(
        {
            "version": {"number": "8.13.0", "build_flavor": "default"},
            "tagline": "You Know, for Search",
        }
    )
    with pytest.raises(ServerValidationError, match="not OpenSearch"):
        validate_server_config(_cfg("opensearch"), client=client)


def test_validate_rejects_unsupported_versions() -> None:
    with pytest.raises(ServerValidationError, match="not supported"):
        validate_server_config(
            _cfg("opensearch"),
            client=_FakeClient({"version": {"distribution": "opensearch", "number": "1.3.0"}}),
        )
    with pytest.raises(ServerValidationError, match="not supported"):
        validate_server_config(
            _cfg("elasticsearch"),
            client=_FakeClient(
                {
                    "version": {"number": "7.17.0", "build_flavor": "default"},
                    "tagline": "You Know, for Search",
                }
            ),
        )


def test_validate_error_never_contains_exception_text() -> None:
    """A client exception's own ``str()`` can echo request URLs/headers --
    validation must only ever report the exception *type*.
    """

    class AuthenticationException(Exception):
        status_code = 401

    exc = AuthenticationException(f"Basic {_SECRET_PASSWORD} rejected at https://u:p@h")
    with pytest.raises(ServerValidationError) as info:
        validate_server_config(_cfg("opensearch"), client=_FakeClient({}, raise_on_info=exc))
    message = str(info.value)
    assert "authentication" in message
    assert _SECRET_PASSWORD not in message and "u:p@" not in message


def test_validate_rejects_userinfo_url_without_echo() -> None:
    config = ServerStorageConfig.model_construct(
        engine="opensearch",
        url=f"https://{_SECRET_USER}:{_SECRET_PASSWORD}@h:9200",
        index_prefix="ragmonk",
        verify_tls=True,
        request_timeout_seconds=30.0,
        bulk=ServerStorageConfig().bulk,
    )
    with pytest.raises(ServerValidationError) as info:
        validate_server_config(config, client=_FakeClient({}))
    assert _SECRET_PASSWORD not in str(info.value)
