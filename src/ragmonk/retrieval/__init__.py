"""Phase 5: Retrieval -- query planning, lexical search, graph retrieval,
context assembly, and the semantic-search seam for Phase 9.

Nothing here talks to a Tree-sitter grammar, a Docling backend, or the
cross-domain linker directly; those stay Phase 2/3/4's job. This package
only orchestrates and ranks what they already store.
"""

from __future__ import annotations
