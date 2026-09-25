"""``KnowledgeBackend`` abstraction (Storage backend abstraction plan, Phase 1).

This package is the scaffolding for making retrieval/indexing storage
backend-agnostic: today the only real implementation is
:class:`ragmonk.backends.local.LocalKnowledgeBackend`, a thin compatibility
wrapper around the existing local SQLite+FTS5+USearch code paths. Server
backends (OpenSearch, Elasticsearch) are a future phase --
:func:`ragmonk.backends.factory.create_backend` raises a clear error for
them today rather than pretending to support them.

Nothing in this package imports an optional server-client library
(``opensearch-py``, ``elasticsearch-py``) at module import time -- see
``factory.py`` and ``base.py`` for where those stay lazy/``TYPE_CHECKING``
only, so local-mode users never need them installed.
"""

from __future__ import annotations
