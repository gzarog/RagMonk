"""``ragmonk init`` storage-mode scaffolding (Storage backend abstraction
plan, Phase 1, P2): backward-compatible default behavior, and real HTTP
preflight validation for server-mode init.
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silence test output
        pass


@pytest.fixture
def stub_http_server() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_init_no_options_is_backward_compatible(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    config_path = paths.user_config_path(ragmonk_home)
    assert config_path.is_file()
    data = yaml.safe_load(config_path.read_text())
    assert data["storage"]["mode"] == "local"
    # Same shape a pre-phase RagMonkConfig().model_dump() would have for
    # every section this phase didn't touch.
    assert data["runtime"]["log_level"] == "info"


def test_init_explicit_local_flag_matches_default(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["init", "--local"])
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(paths.user_config_path(ragmonk_home).read_text())
    assert data["storage"]["mode"] == "local"


def test_init_server_mode_unreachable_url_fails_cleanly(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    config_path = paths.user_config_path(ragmonk_home)
    result = runner.invoke(
        app,
        [
            "init",
            "--storage-mode",
            "server",
            "--storage-engine",
            "opensearch",
            "--storage-url",
            "http://127.0.0.1:1/",  # nothing listens here
        ],
    )
    assert result.exit_code != 0
    assert not config_path.is_file(), "no config file must be written on preflight failure"


def test_init_server_mode_reachable_stub_succeeds(
    ragmonk_home: Path, runner: CliRunner, stub_http_server: str
) -> None:
    config_path = paths.user_config_path(ragmonk_home)
    result = runner.invoke(
        app,
        [
            "init",
            "--storage-mode",
            "server",
            "--storage-engine",
            "opensearch",
            "--storage-url",
            stub_http_server,
            "--storage-index-prefix",
            "myproj",
        ],
    )
    assert result.exit_code == 0, result.output
    assert config_path.is_file()

    raw = config_path.read_text()
    data = yaml.safe_load(raw)
    assert data["storage"]["mode"] == "server"
    assert data["storage"]["server"]["engine"] == "opensearch"
    assert data["storage"]["server"]["url"] == stub_http_server
    assert data["storage"]["server"]["index_prefix"] == "myproj"

    for forbidden in ("username", "password", "api_key", "Authorization"):
        assert forbidden not in raw, f"credential-shaped key {forbidden!r} leaked into config"


def test_init_server_mode_requires_url(ragmonk_home: Path, runner: CliRunner) -> None:
    config_path = paths.user_config_path(ragmonk_home)
    result = runner.invoke(app, ["init", "--storage-mode", "server"])
    assert result.exit_code != 0
    assert not config_path.is_file()


def test_init_existing_config_unaffected_by_storage_flags(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    """Storage flags only apply to a *new* config; re-running init against
    an existing config just validates it, exactly as before this phase.
    """
    assert runner.invoke(app, ["init"]).exit_code == 0
    config_path = paths.user_config_path(ragmonk_home)
    before = config_path.read_text()

    result = runner.invoke(
        app, ["init", "--storage-mode", "server", "--storage-url", "http://127.0.0.1:1/"]
    )
    assert result.exit_code == 0, result.output
    assert config_path.read_text() == before
