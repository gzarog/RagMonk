"""Storage backend abstraction plan, Phase 1: config-layer tests for
``storage.*``. Mirrors the pattern in ``test_config.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ragmonk.core.config import RagMonkConfig, load_config
from ragmonk.core.errors import ConfigError


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_storage_defaults_to_local(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.storage.mode == "local"
    assert config.storage.server.engine == "opensearch"


def test_old_config_without_storage_key_still_loads_as_local(tmp_path: Path) -> None:
    """Backward compatibility: a config file written before this phase has
    no ``storage`` key at all -- it must still load with mode == 'local'.
    """
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _write_yaml(home / "config.yaml", {"runtime": {"log_level": "warning"}})
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.storage.mode == "local"
    assert config.runtime.log_level == "warning"


def test_storage_mode_env_var_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(
        home=home,
        cwd=cwd,
        environ={
            "RAGMONK_STORAGE__MODE": "server",
            "RAGMONK_STORAGE__SERVER__ENGINE": "elasticsearch",
        },
    )
    assert config.storage.mode == "server"
    assert config.storage.server.engine == "elasticsearch"


def test_storage_server_nested_config_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(
        home / "config.yaml",
        {
            "storage": {
                "mode": "server",
                "server": {
                    "engine": "opensearch",
                    "url": "https://search.example.internal:9200",
                    "index_prefix": "myproj",
                    "verify_tls": False,
                    "bulk": {"max_actions": 100, "concurrency": 4},
                },
            }
        },
    )
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.storage.mode == "server"
    assert config.storage.server.url == "https://search.example.internal:9200"
    assert config.storage.server.index_prefix == "myproj"
    assert config.storage.server.verify_tls is False
    assert config.storage.server.bulk.max_actions == 100
    assert config.storage.server.bulk.concurrency == 4
    # Untouched bulk fields keep their defaults.
    assert config.storage.server.bulk.max_bytes == 5_000_000


def test_invalid_storage_engine_fails_validation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(
        home / "config.yaml",
        {"storage": {"mode": "server", "server": {"engine": "solr"}}},
    )
    with pytest.raises(ConfigError, match="storage.server.engine"):
        load_config(home=home, cwd=cwd, environ={})


def test_invalid_storage_mode_fails_validation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"storage": {"mode": "cloud"}})
    with pytest.raises(ConfigError, match="storage.mode"):
        load_config(home=home, cwd=cwd, environ={})


def test_storage_config_never_has_credential_fields() -> None:
    """Locks the contract: credentials are never part of this model, so
    they can never leak into a written config.yaml.
    """
    config = RagMonkConfig()
    dumped = config.model_dump(mode="json")
    server_dump = dumped["storage"]["server"]
    for forbidden in ("username", "password", "api_key"):
        assert forbidden not in server_dump


# -- Completion plan F7: credential-bearing URLs are rejected ----------------

_F7_USER = "leaky-user"
_F7_PASSWORD = "leaky-password-value"  # noqa: S105 - test fixture value


@pytest.mark.parametrize(
    "url",
    [
        f"https://{_F7_USER}:{_F7_PASSWORD}@search.example:9200",
        f"http://{_F7_USER}@search.example:9200",
        f"{_F7_USER}:{_F7_PASSWORD}@search.example:9200",
    ],
)
def test_server_config_rejects_userinfo_in_url(url: str) -> None:
    from ragmonk.core.config import ServerStorageConfig

    with pytest.raises(ValueError, match="must not contain credentials"):
        ServerStorageConfig(url=url)


def test_server_config_accepts_plain_urls() -> None:
    from ragmonk.core.config import ServerStorageConfig

    for url in ("https://search.example:9200", "http://127.0.0.1:9200/prefix", ""):
        assert ServerStorageConfig(url=url).url == url


def test_load_config_error_for_userinfo_url_never_echoes_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.core.config import load_config
    from ragmonk.core.errors import ConfigError

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "storage:\n  mode: server\n  server:\n"
        f"    url: https://{_F7_USER}:{_F7_PASSWORD}@search.example:9200\n"
    )
    monkeypatch.setenv("RAGMONK_HOME", str(home))
    with pytest.raises(ConfigError) as info:
        load_config(home=home)
    message = str(info.value)
    assert "must not contain credentials" in message
    assert _F7_PASSWORD not in message and _F7_USER not in message


def test_env_override_userinfo_url_is_rejected_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ragmonk.core.config import load_config
    from ragmonk.core.errors import ConfigError

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAGMONK_STORAGE__MODE", "server")
    monkeypatch.setenv(
        "RAGMONK_STORAGE__SERVER__URL", f"https://{_F7_USER}:{_F7_PASSWORD}@h:9200"
    )
    with pytest.raises(ConfigError) as info:
        load_config(home=home)
    assert _F7_PASSWORD not in str(info.value)
