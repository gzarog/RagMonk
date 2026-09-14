"""``ragpilot install-agent [--write PATH] [--client NAME ...]``.

Blueprint section 74 names this command "connect AI agent" but gives no
further behavior. Two modes:

- No ``--client``: unchanged from the original, deliberately modest and
  safe design -- PRINTS the standard ``{"mcpServers": {"ragpilot": {...}}}``
  JSON snippet most MCP clients expect for registering a local stdio MCP
  server, and, only with an explicit ``--write PATH``, writes that same
  snippet to the given file. It never searches for, discovers, or edits
  a real client config file on its own.
- ``--client NAME`` (repeatable, or ``all``): automatically registers
  with that client's *real* config file, at its documented, fixed
  location -- this is an explicit, deliberate escalation from "print/
  write-only-when-named" to "actually set this client up", requested and
  scoped to exactly these four clients. It still keeps the same safety
  spirit in a different form: no config file is *discovered* by
  searching the filesystem (every path is a hardcoded, documented
  location for that specific client), and only the single ``ragpilot``
  entry within that file is ever added or updated -- every other key,
  server, or setting already in the file is preserved untouched. See
  ``_merge_json_client``/``_merge_toml_client`` for exactly how.

Per-client config location and format (verified against each vendor's
current docs, not assumed from memory):

- Claude Code: project-scope ``.mcp.json`` (cwd), ``{"mcpServers": {...}}``,
  each entry needs an explicit ``"type": "stdio"``.
- Cursor: project-scope ``.cursor/mcp.json`` (cwd), same ``mcpServers``
  shape as Claude Code, but no ``"type"`` field in its own examples.
- VS Code: workspace ``.vscode/mcp.json`` (cwd), top-level key is
  ``servers`` (not ``mcpServers``), entries use ``"type": "stdio"``.
- Codex: user-level ``~/.codex/config.toml`` (TOML, not JSON) --
  project-level ``.codex/config.toml`` exists too, but only takes effect
  for a project Codex has separately marked "trusted"; the user-level
  file works unconditionally, so that's the one used here.

All four are project/workspace-scoped except Codex -- consistent with
each tool's own recommended/most broadly-effective location, not forced
uniformity for its own sake.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ragpilot.core.errors import UsageError

from ._common import cli_command, console, error_console


def _snippet() -> dict[str, object]:
    return {
        "mcpServers": {
            "ragpilot": {
                "command": "ragpilot",
                "args": ["serve", "--mcp"],
            }
        }
    }


class ClientTarget(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    CURSOR = "cursor"
    VSCODE = "vscode"
    ALL = "all"


_JSON_ENTRY: dict[str, object] = {"command": "ragpilot", "args": ["serve", "--mcp"]}
_JSON_ENTRY_WITH_TYPE: dict[str, object] = {"type": "stdio", **_JSON_ENTRY}
_TOML_BLOCK = '\n[mcp_servers.ragpilot]\ncommand = "ragpilot"\nargs = ["serve", "--mcp"]\n'


def _merge_json_client(path: Path, *, top_level_key: str, include_type: bool) -> str:
    """Merges just ``<top_level_key>.ragpilot`` into ``path``'s existing
    JSON, preserving every other key/server already there -- never a
    blind overwrite, the same safety property ``--write`` already had
    for a user-named path. A single dict-key replace is always
    structurally safe in JSON (unlike TOML's duplicate-table problem,
    see ``_merge_toml_client``), so a *different* pre-existing
    ``ragpilot`` entry is corrected in place rather than left alone --
    re-running this command is also how a stale entry gets repaired.
    """
    entry = _JSON_ENTRY_WITH_TYPE if include_type else _JSON_ENTRY

    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"skipped ({path}: could not read/parse existing file: {exc})"
        if not isinstance(data, dict):
            return f"skipped ({path}: existing content is not a JSON object)"
    else:
        data = {}

    section = data.get(top_level_key)
    if not isinstance(section, dict):
        section = {}
    if section.get("ragpilot") == entry:
        return f"already configured ({path})"
    was_present = "ragpilot" in section
    section["ragpilot"] = entry
    data[top_level_key] = section

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"{'updated' if was_present else 'added'} ({path})"


def _merge_toml_client(path: Path) -> str:
    """Codex's ``config.toml`` has no first-party writer in the standard
    library (``tomllib`` only reads) -- rather than adding a dependency
    or hand-rolling a full TOML serializer (either risks losing comments/
    formatting elsewhere in a file this command did not create),
    ``[mcp_servers.ragpilot]`` is appended as raw text, only when that
    table is not already present. Unlike the JSON case, a table cannot
    simply be replaced by appending a second declaration of it -- TOML
    treats a redefined table as a parse error -- so a pre-existing
    ``ragpilot`` entry that doesn't already match is left alone and
    reported instead of risking a corrupt file.
    """
    if path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
            data = tomllib.loads(raw)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return f"skipped ({path}: could not read/parse existing file: {exc})"
        existing = data.get("mcp_servers", {}).get("ragpilot")
        if existing == {"command": "ragpilot", "args": ["serve", "--mcp"]}:
            return f"already configured ({path})"
        if existing is not None:
            return (
                f"skipped ({path}: a different [mcp_servers.ragpilot] entry "
                "already exists -- edit it manually)"
            )
    else:
        raw = ""

    path.parent.mkdir(parents=True, exist_ok=True)
    separator = "" if not raw or raw.endswith("\n") else "\n"
    path.write_text(raw + separator + _TOML_BLOCK, encoding="utf-8")
    return f"added ({path})"


@dataclass(frozen=True)
class _ClientConfig:
    label: str
    resolve_path: Callable[[], Path]
    apply: Callable[[Path], str]


_CLIENT_CONFIGS: dict[ClientTarget, _ClientConfig] = {
    ClientTarget.CLAUDE_CODE: _ClientConfig(
        label="Claude Code",
        resolve_path=lambda: Path.cwd() / ".mcp.json",
        apply=lambda path: _merge_json_client(path, top_level_key="mcpServers", include_type=True),
    ),
    ClientTarget.CURSOR: _ClientConfig(
        label="Cursor",
        resolve_path=lambda: Path.cwd() / ".cursor" / "mcp.json",
        apply=lambda path: _merge_json_client(
            path, top_level_key="mcpServers", include_type=False
        ),
    ),
    ClientTarget.VSCODE: _ClientConfig(
        label="VS Code",
        resolve_path=lambda: Path.cwd() / ".vscode" / "mcp.json",
        apply=lambda path: _merge_json_client(path, top_level_key="servers", include_type=True),
    ),
    ClientTarget.CODEX: _ClientConfig(
        label="Codex",
        resolve_path=lambda: Path.home() / ".codex" / "config.toml",
        apply=_merge_toml_client,
    ),
}


@cli_command
def install_agent(
    write: Annotated[
        Path | None,
        typer.Option(
            "--write",
            help=(
                "Also write the generic snippet to this exact file path. No "
                "existing MCP client config is discovered or edited "
                "automatically. Mutually exclusive with --client."
            ),
        ),
    ] = None,
    client: Annotated[
        list[ClientTarget],
        typer.Option(
            "--client",
            help=(
                "Automatically register with this client's real config "
                "file, at its documented location (repeatable; 'all' for "
                "every supported client: claude-code, codex, cursor, "
                "vscode). Only the 'ragpilot' entry in that file is added "
                "or updated -- everything else already there is left "
                "untouched. Omit for the plain print/--write-only "
                "behavior above."
            ),
        ),
    ] = [],  # noqa: B006 - typer needs a literal default; never mutated
) -> None:
    if client and write is not None:
        raise UsageError("--client and --write are mutually exclusive")

    if client:
        targets = _CLIENT_CONFIGS.keys() if ClientTarget.ALL in client else dict.fromkeys(client)
        for target in targets:
            config = _CLIENT_CONFIGS[target]
            status = config.apply(config.resolve_path())
            # status carries an interpolated Path and, in one message, a
            # literal "[mcp_servers.ragpilot]" -- escaped so Rich's markup
            # parser doesn't mistake either for a style tag and silently
            # swallow it (confirmed live: it did, dropping the bracketed
            # text entirely without escaping).
            console.print(f"[bold]{config.label}:[/bold] {escape(status)}")
        return

    # Plain ``print`` (not the Rich console) so stdout carries exactly this
    # JSON and nothing else -- pipeable (``ragpilot install-agent > x.json``)
    # and stable for tests, matching cli/_common.py's ``print_json`` for the
    # same reason.
    snippet = json.dumps(_snippet(), indent=2)
    print(snippet)

    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(snippet + "\n", encoding="utf-8")
        error_console.print(f"[bold green]Wrote[/bold green] {write}")
