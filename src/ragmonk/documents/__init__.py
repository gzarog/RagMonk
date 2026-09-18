"""Phase 3: Docling-backed document ingestion.

``docling_adapter`` wraps Docling's Python API, ``normalizer`` turns its
``DoclingDocument`` into RagMonk's own hierarchy-aware unit list,
``chunker`` groups that into size-bounded storable/indexable units, and
``metadata`` extracts document-level metadata. ``pipeline`` wires all four
into the ``DocumentProcessor`` registered against ``FileKind.DOCUMENT`` in
``indexing.coordinator.ProcessorRegistry``, mirroring Phase 2's
``code/processor.py``.
"""

from __future__ import annotations
