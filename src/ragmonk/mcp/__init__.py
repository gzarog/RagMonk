"""Phase 6: MCP server -- exposes the same read-only retrieval RagMonk's
CLI already provides (search/symbol/callers/callees/impact/documents/
status/explore) to MCP-speaking agents over stdio, with bounded
responses, deterministic schemas, and a per-call timeout.
"""

from __future__ import annotations
