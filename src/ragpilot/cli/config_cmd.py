"""``ragpilot config show|get|set``."""

from __future__ import annotations

from typing import Annotated, Any

import typer
import yaml

from ragpilot.core.config import RagpilotConfig, load_config, write_user_config
from ragpilot.core.errors import UsageError
from ragpilot.core.paths import ensure_runtime_layout

from ._common import cli_command, console

app = typer.Typer(no_args_is_help=True, help="Inspect and edit configuration.")


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    cursor: Any = data
    for segment in dotted.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            raise UsageError(f"unknown config key: {dotted}")
        cursor = cursor[segment]
    return cursor


@app.command("show")
@cli_command
def show() -> None:
    home = ensure_runtime_layout()
    config = load_config(home=home)
    console.print(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False))


@app.command("get")
@cli_command
def get(
    key: Annotated[str, typer.Argument(help="Dotted config key, e.g. runtime.log_level")],
) -> None:
    home = ensure_runtime_layout()
    config = load_config(home=home)
    value = _get_path(config.model_dump(mode="json"), key)
    console.print(value)


@app.command("set")
@cli_command
def set_value(
    key: Annotated[str, typer.Argument(help="Dotted config key, e.g. runtime.log_level")],
    value: Annotated[str, typer.Argument()],
) -> None:
    home = ensure_runtime_layout()
    config = load_config(home=home)
    data = config.model_dump(mode="json")

    segments = key.split(".")
    cursor = data
    for segment in segments[:-1]:
        if segment not in cursor or not isinstance(cursor[segment], dict):
            raise UsageError(f"unknown config key: {key}")
        cursor = cursor[segment]
    last = segments[-1]
    if last not in cursor:
        raise UsageError(f"unknown config key: {key}")
    current = cursor[last]
    if isinstance(current, bool):
        cursor[last] = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        cursor[last] = int(value)
    elif isinstance(current, float):
        cursor[last] = float(value)
    elif isinstance(current, list):
        # A comma-separated string, e.g. `ragpilot config set
        # search.output.fallback snippets,json,files` -- the only list-typed
        # config field today; re-validated against its Pydantic model right
        # below, same as every other type here.
        cursor[last] = [item.strip() for item in value.split(",") if item.strip()]
    else:
        cursor[last] = value

    updated = RagpilotConfig.model_validate(data)
    path = write_user_config(updated, home=home)
    console.print(f"Set [cyan]{key}[/cyan] = {cursor[last]!r} in {path}")
