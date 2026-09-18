"""``ragmonk version [--json]``."""

from __future__ import annotations

import platform
from typing import Annotated

import typer

from ragmonk import __version__

from ._common import cli_command, console, print_json


@cli_command
def version(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    data = {"version": __version__, "python": platform.python_version()}
    if json_output:
        print_json(data)
    else:
        console.print(f"ragmonk {__version__}")
