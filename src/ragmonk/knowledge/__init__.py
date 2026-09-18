"""Phase 4: the unified knowledge model.

Connects Phase 2's code graph to Phase 3's document store -- cross-domain
linking (``linker.py``), a normalized evidence shape for either domain
(``evidence.py``), a shared confidence ladder (``confidence.py``), and a
lookup facade over both domains' entities (``entities.py``). See
``linker.py``'s module docstring for the confidence/resolver rules and
``cli/index.py`` for where the linking pass runs.
"""

from __future__ import annotations
