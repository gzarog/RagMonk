"""Job type vocabulary used by the durable queue.

Phase 1 only ever enqueues ``INDEX_FILE`` jobs; the enum exists so later
phases (re-parse, re-embed, ...) can add job types without touching the
queue plumbing in ``storage.repositories.jobs_repo``.
"""

from __future__ import annotations

from ragmonk.core.models import JobType

__all__ = ["JobType"]
