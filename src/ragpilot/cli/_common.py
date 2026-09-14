"""Shared CLI plumbing: JSON envelope, error-to-exit-code mapping."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

import typer
from rich.console import Console

from ragpilot.core.errors import EXIT_GENERIC_FAILURE, RagpilotError

console = Console()
error_console = Console(stderr=True)

SCHEMA_VERSION = "1"


def json_envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "data": data}


def print_json(data: dict[str, Any]) -> None:
    # ensure_ascii=False: json.dumps defaults to escaping every non-ASCII
    # character as \uXXXX, which turns any non-Latin indexed content
    # (Greek, Cyrillic, CJK, accented Latin, ...) into unreadable escape
    # sequences in --json output -- this project already writes UTF-8
    # everywhere else (config.yaml, install_info.json), so JSON output
    # should be no different.
    print(json.dumps(json_envelope(data), indent=2, default=str, ensure_ascii=False))


def cli_command[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return func(*args, **kwargs)
        except RagpilotError as exc:
            error_console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=exc.exit_code) from exc
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 - CLI boundary: never traceback to the user
            error_console.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(code=EXIT_GENERIC_FAILURE) from exc

    return wrapper
