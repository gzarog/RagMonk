"""Typed, validated configuration editing for the admin UI.

Admin UI plan, Phase 6 (§9): expose ``RagMonkConfig`` as an editable form
that validates through Pydantic exactly like the CLI, surfaces
environment-variable overrides as read-only, and never writes arbitrary
YAML keys. All changes go through ``RagMonkConfig.model_validate`` and
``write_user_config`` -- the same path ``ragmonk config`` uses.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig, load_config, write_user_config
from ragmonk.core.lifecycle import AppContext

_ENV_PREFIX = "RAGMONK_"

# Field paths whose *value* must never be rendered. RagMonk keeps real
# credentials out of config entirely (API keys are env-only -- see
# AiConfig), but this list is the explicit belt-and-braces guard the plan
# (§9.4) asks for: anything matching is shown as Configured/Not configured,
# never its value.
_SENSITIVE_SUBSTRINGS = ("key", "token", "secret", "password", "credential")


def _scalar_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "list"
    return None


def _is_sensitive(path: str) -> bool:
    lowered = path.lower()
    return any(token in lowered for token in _SENSITIVE_SUBSTRINGS)


def _env_var_for(path: str) -> str:
    return _ENV_PREFIX + path.replace(".", "__").upper()


def _describe_model(
    model: BaseModel, prefix: str, environ: dict[str, str]
) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for name, info in type(model).model_fields.items():
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            # One level of nesting is rendered as its own subsection.
            fields.append(
                {
                    "name": name,
                    "path": path,
                    "kind": "group",
                    "fields": _describe_model(value, path, environ),
                }
            )
            continue
        kind = _scalar_kind(value)
        if kind is None:
            continue
        env_var = _env_var_for(path)
        overridden = env_var in environ
        sensitive = _is_sensitive(path)
        default = info.default if info.default is not PydanticUndefined else None
        fields.append(
            {
                "name": name,
                "path": path,
                "kind": kind,
                "value": None if sensitive else value,
                "display": _display_value(value, kind, sensitive),
                "default": None if sensitive else default,
                "sensitive": sensitive,
                "env_overridden": overridden,
                "env_var": env_var if overridden else None,
                "editable": not overridden and not sensitive,
            }
        )
    return fields


def _display_value(value: Any, kind: str, sensitive: bool) -> str:
    if sensitive:
        return "Configured" if value else "Not configured"
    if kind == "list":
        return ", ".join(value)
    return str(value)


def describe_config(ctx: AppContext) -> list[dict[str, Any]]:
    """Every config section with its effective values and env overrides."""
    environ = dict(os.environ)
    sections: list[dict[str, Any]] = []
    for name in type(ctx.config).model_fields:
        value = getattr(ctx.config, name)
        if isinstance(value, BaseModel):
            sections.append(
                {
                    "name": name,
                    "fields": _describe_model(value, name, environ),
                }
            )
    return sections


def _coerce(kind: str, raw: str) -> Any:
    raw = raw.strip()
    if kind == "bool":
        return raw.lower() in ("true", "1", "on", "yes")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "list":
        return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]
    return raw


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = target
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def apply_updates(ctx: AppContext, form: dict[str, str]) -> RagMonkConfig:
    """Validate and persist form updates to the user config file.

    ``form`` maps ``section__field`` (or ``section__group__field``) keys
    to raw string values. Sensitive and env-overridden fields are ignored
    even if present. The merged config is validated by
    ``RagMonkConfig.model_validate`` before it is written -- an invalid
    change raises and nothing is persisted.
    """
    # Start from the current user-file layer only (not the fully merged,
    # env-influenced runtime config) so we never bake an env override into
    # the file on disk.
    from ragmonk.core.config import _load_yaml_file  # noqa: PLC0415 - internal reuse

    user_layer = _load_yaml_file(paths.user_config_path(ctx.home))
    kinds = _kinds_by_path(ctx)
    environ = dict(os.environ)

    for key, raw in form.items():
        path = key.replace("__", ".")
        kind = kinds.get(path)
        if kind is None or kind == "group":
            continue
        if _is_sensitive(path) or _env_var_for(path) in environ:
            continue
        _set_path(user_layer, path, _coerce(kind, raw))

    # Validate the *full* effective config (defaults + edited user layer),
    # then persist. load_config already applies defaults + this file.
    merged = RagMonkConfig().model_dump(mode="json")
    merged = _deep_merge(merged, user_layer)
    validated = RagMonkConfig.model_validate(merged)
    write_user_config(validated, home=ctx.home)
    # Return the freshly reloaded runtime view (re-applies env layer).
    return load_config(home=ctx.home, cwd=ctx.cwd)


def _kinds_by_path(ctx: AppContext) -> dict[str, str]:
    kinds: dict[str, str] = {}

    def walk(section: dict[str, Any]) -> None:
        for field in section["fields"]:
            if field["kind"] == "group":
                walk(field)
            else:
                kinds[field["path"]] = field["kind"]

    for section in describe_config(ctx):
        walk(section)
    return kinds


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
