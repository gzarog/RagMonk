"""Completion plan F7: secret-leak audit.

Credentials may only come from the ``RAGMONK_<ENGINE>_*`` env vars. With
those set to recognizable values, no command surface -- client
construction, ``status``, ``doctor``, indexing with bulk failures, the
top-level CLI error boundary -- may ever print them, and a
credential-bearing URL is refused everywhere without being echoed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.integration._server_harness import (
    ENGINES,
    add_source,
    install_server_mode,
    write_project,
)
from typer.testing import CliRunner

from ragmonk.cli.main import app

_USER = "leak-canary-user"
_PASSWORD = "leak-canary-password"  # noqa: S105 - test fixture value
_API_KEY = "leak-canary-api-key"


@pytest.fixture(params=ENGINES)
def engine(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _set_credentials(monkeypatch: pytest.MonkeyPatch, engine: str) -> None:
    prefix = f"RAGMONK_{engine.upper()}"
    monkeypatch.setenv(f"{prefix}_USERNAME", _USER)
    monkeypatch.setenv(f"{prefix}_PASSWORD", _PASSWORD)
    monkeypatch.setenv(f"{prefix}_API_KEY", _API_KEY)


def _assert_clean(text: str) -> None:
    for secret in (_USER, _PASSWORD, _API_KEY):
        assert secret not in text, f"secret {secret!r} leaked"


@pytest.mark.parametrize("engine_name", ["opensearch", "elasticsearch"])
def test_client_construction_refuses_userinfo_url_without_echo(engine_name: str) -> None:
    module = __import__(f"ragmonk.backends.{engine_name}_client", fromlist=["build_client"])
    with pytest.raises(Exception) as info:
        module.build_client(
            url=f"https://{_USER}:{_PASSWORD}@h:9200",
            verify_tls=True,
            request_timeout_seconds=5,
            username_var="X_U",
            password_var="X_P",
            api_key_var="X_K",
        )
    _assert_clean(str(info.value))


def test_status_doctor_index_and_errors_never_print_credentials(
    ragmonk_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    engine: str,
) -> None:
    _set_credentials(monkeypatch, engine)
    fake = install_server_mode(ragmonk_home, monkeypatch, engine)
    project = tmp_path / "proj"
    write_project(project)
    add_source(runner, project)

    outputs: list[str] = []
    for args in (["index"], ["status"], ["status", "--json"], ["doctor"], ["doctor", "--json"]):
        result = runner.invoke(app, args)
        outputs.append(result.output)

    # Outage during indexing: a raised client error carrying the secret in
    # its own message must be redacted/contained at the boundary.
    def _outage(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError(f"connection refused for {_USER}:{_PASSWORD}@h and {_API_KEY}")

    monkeypatch.setattr(fake, "search", _outage)
    monkeypatch.setattr(fake, "get", _outage)
    for args in (["index"], ["search", "compute_invoice_total"], ["status"]):
        result = runner.invoke(app, args)
        outputs.append(result.output)
        if result.exception is not None and not isinstance(result.exception, SystemExit):
            outputs.append(str(result.exception))

    _assert_clean("\n".join(outputs))


def test_config_file_with_userinfo_url_is_rejected_by_every_command(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    ragmonk_home.mkdir(parents=True, exist_ok=True)
    (ragmonk_home / "config.yaml").write_text(
        "storage:\n  mode: server\n  server:\n    engine: opensearch\n"
        f"    url: https://{_USER}:{_PASSWORD}@h:9200\n"
    )
    for args in (["status"], ["doctor"], ["index"], ["search", "x"]):
        result = runner.invoke(app, args)
        assert result.exit_code != 0, (args, result.output)
        _assert_clean(result.output)
