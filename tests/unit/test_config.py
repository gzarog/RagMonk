from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ragmonk.core.config import RagMonkConfig, load_config
from ragmonk.core.errors import ConfigError


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_defaults_used_when_nothing_else_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.runtime.log_level == "info"
    assert config.indexing.max_file_size_mb == 100


def test_user_config_overrides_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    _write_yaml(home / "config.yaml", {"runtime": {"log_level": "warning"}})
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.runtime.log_level == "warning"


def test_project_config_overrides_user_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"runtime": {"log_level": "warning"}})
    _write_yaml(cwd / ".ragmonk.yaml", {"runtime": {"log_level": "error"}})
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.runtime.log_level == "error"


def test_env_var_overrides_project_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"runtime": {"log_level": "warning"}})
    _write_yaml(cwd / ".ragmonk.yaml", {"runtime": {"log_level": "error"}})
    config = load_config(
        home=home, cwd=cwd, environ={"RAGMONK_RUNTIME__LOG_LEVEL": "debug"}
    )
    assert config.runtime.log_level == "debug"


def test_cli_override_wins_over_everything(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"runtime": {"log_level": "warning"}})
    _write_yaml(cwd / ".ragmonk.yaml", {"runtime": {"log_level": "error"}})
    config = load_config(
        home=home,
        cwd=cwd,
        environ={"RAGMONK_RUNTIME__LOG_LEVEL": "debug"},
        cli_overrides={"runtime": {"log_level": "critical"}},
    )
    assert config.runtime.log_level == "critical"


def test_updates_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.updates.enabled is True
    assert config.updates.check_interval_hours == 24
    assert config.updates.notify is True
    assert config.updates.channel == "stable"


def test_updates_env_var_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(
        home=home,
        cwd=cwd,
        environ={
            "RAGMONK_UPDATES__ENABLED": "false",
            "RAGMONK_UPDATES__CHECK_INTERVAL_HOURS": "6",
        },
    )
    assert config.updates.enabled is False
    assert config.updates.check_interval_hours == 6


def test_env_var_type_coercion(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(
        home=home,
        cwd=cwd,
        environ={
            "RAGMONK_RUNTIME__MAX_WORKERS": "12",
            "RAGMONK_INDEXING__FOLLOW_SYMLINKS": "true",
        },
    )
    assert config.runtime.max_workers == 12
    assert config.indexing.follow_symlinks is True


def test_search_defaults_preserve_existing_semantic_behavior(tmp_path: Path) -> None:
    """Blueprint sections 18/31/32: the new knobs default to what the
    codebase already did before this redesign -- ``lazy_semantic`` off
    (semantic search always attached once ``search.semantic`` is on) and
    ``vector.engine="auto"`` (tries USearch, falls back to brute-force).
    """
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.search.lazy_semantic is False
    assert config.search.semantic_top_k == 30
    assert config.search.vector.engine == "auto"
    assert config.search.vector.rebuild_deleted_ratio == 0.15
    assert config.search.cache.enabled is True
    assert config.search.cache.max_queries == 256


def test_nested_search_vector_env_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    config = load_config(
        home=home,
        cwd=cwd,
        environ={"RAGMONK_SEARCH__VECTOR__ENGINE": "bruteforce"},
    )
    assert config.search.vector.engine == "bruteforce"


def test_unrelated_env_vars_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(
        home=home, cwd=cwd, environ={"RAGMONK_HOME": "/somewhere", "PATH": "/bin"}
    )
    assert config.runtime.log_level == "info"


def test_search_output_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.search.output.fallback == ["snippets", "json", "files"]
    assert config.search.output.snippet_max_tokens == 32


def test_search_output_fallback_configurable_via_user_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"search": {"output": {"fallback": ["json", "files"]}}})
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.search.output.fallback == ["json", "files"]


def test_search_output_fallback_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown search.output.fallback mode"):
        RagMonkConfig.model_validate({"search": {"output": {"fallback": ["bogus"]}}})


def test_search_output_fallback_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        RagMonkConfig.model_validate({"search": {"output": {"fallback": []}}})


@pytest.mark.parametrize("value", [0, 65, -1])
def test_search_output_snippet_max_tokens_out_of_range(value: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 64"):
        RagMonkConfig.model_validate({"search": {"output": {"snippet_max_tokens": value}}})


def test_search_output_invalid_config_file_raises_config_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"search": {"output": {"fallback": ["bogus"]}}})
    with pytest.raises(ConfigError):
        load_config(home=home, cwd=cwd, environ={})


def test_documents_chunking_defaults(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.documents.chunking.strategy == "hybrid"
    assert config.documents.chunking.max_tokens == 350
    assert config.documents.chunking.min_tokens == 60
    assert config.documents.chunking.overlap_tokens == 40
    assert config.documents.chunking.merge_peers is True


def test_documents_chunking_configurable_via_user_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(
        home / "config.yaml",
        {"documents": {"chunking": {"max_tokens": 500, "merge_peers": False}}},
    )
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.documents.chunking.max_tokens == 500
    assert config.documents.chunking.merge_peers is False
    # Untouched siblings keep their defaults.
    assert config.documents.chunking.min_tokens == 60


def test_documents_chunking_env_var_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(
        home=home,
        cwd=cwd,
        environ={"RAGMONK_DOCUMENTS__CHUNKING__MAX_TOKENS": "200"},
    )
    assert config.documents.chunking.max_tokens == 200


def test_documents_chunking_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown documents.chunking.strategy"):
        RagMonkConfig.model_validate({"documents": {"chunking": {"strategy": "bogus"}}})


def test_documents_chunking_rejects_min_tokens_above_max_tokens() -> None:
    with pytest.raises(ValueError, match="min_tokens must not exceed"):
        RagMonkConfig.model_validate(
            {"documents": {"chunking": {"min_tokens": 400, "max_tokens": 350}}}
        )


def test_documents_chunking_rejects_overlap_tokens_at_or_above_max_tokens() -> None:
    with pytest.raises(ValueError, match="overlap_tokens must be less than"):
        RagMonkConfig.model_validate(
            {"documents": {"chunking": {"overlap_tokens": 350, "max_tokens": 350}}}
        )


def test_documents_chunking_rejects_max_tokens_too_small() -> None:
    with pytest.raises(ValueError, match="max_tokens must be at least 16"):
        RagMonkConfig.model_validate({"documents": {"chunking": {"max_tokens": 4}}})


def test_documents_chunking_rejects_negative_overlap_tokens() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        RagMonkConfig.model_validate({"documents": {"chunking": {"overlap_tokens": -1}}})


def test_documents_ocr_defaults_to_auto(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.documents.ocr == "auto"


@pytest.mark.parametrize("value", ["off", "auto", "always"])
def test_documents_ocr_accepts_each_supported_mode(value: str) -> None:
    config = RagMonkConfig.model_validate({"documents": {"ocr": value}})
    assert config.documents.ocr == value


def test_documents_ocr_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown documents.ocr"):
        RagMonkConfig.model_validate({"documents": {"ocr": "bogus"}})


def test_documents_ocr_configurable_via_user_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    _write_yaml(home / "config.yaml", {"documents": {"ocr": "off"}})
    config = load_config(home=home, cwd=cwd, environ={})
    assert config.documents.ocr == "off"


def test_documents_ocr_env_var_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    config = load_config(home=home, cwd=cwd, environ={"RAGMONK_DOCUMENTS__OCR": "always"})
    assert config.documents.ocr == "always"
