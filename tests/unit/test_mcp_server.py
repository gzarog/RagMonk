"""``mcp/server.py``'s ``build_server`` -- pure construction, no stdio/
sockets, so it is exercised directly rather than through a real client.
"""

from __future__ import annotations

import asyncio

from ragmonk import __version__
from ragmonk.mcp.server import build_server

EXPECTED_TOOL_NAMES = {
    "ragmonk_explore",
    "ragmonk_search",
    "ragmonk_symbol",
    "ragmonk_callers",
    "ragmonk_callees",
    "ragmonk_impact",
    "ragmonk_documents",
    "ragmonk_status",
    "ragmonk_ask",
}


def test_build_server_registers_all_nine_tools() -> None:
    server = build_server()

    tool_list = asyncio.run(server.list_tools())
    names = {tool.name for tool in tool_list}

    assert names == EXPECTED_TOOL_NAMES


def test_build_server_carries_version_and_readonly_annotations() -> None:
    server = build_server()

    assert __version__ in server.name
    assert server.instructions is not None
    assert "ragmonk_explore" in server.instructions

    tool_list = asyncio.run(server.list_tools())
    for tool in tool_list:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False


def test_explore_is_marked_as_the_primary_tool_in_its_description() -> None:
    server = build_server()
    tool_list = asyncio.run(server.list_tools())
    explore = next(t for t in tool_list if t.name == "ragmonk_explore")

    assert explore.description is not None
    assert "primary" in explore.description.lower()


def test_ask_is_the_one_open_world_tool() -> None:
    server = build_server()
    tool_list = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tool_list}

    assert by_name["ragmonk_ask"].annotations is not None
    assert by_name["ragmonk_ask"].annotations.openWorldHint is True
    for name, tool in by_name.items():
        if name == "ragmonk_ask":
            continue
        assert tool.annotations is not None
        assert tool.annotations.openWorldHint is False
