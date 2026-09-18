"""Health / doctor results for the admin UI.

Admin UI plan, Phase 9 (§12.4): expose what ``ragmonk doctor`` /
``ragmonk health`` report. Reuses ``cli.doctor.run_checks`` unchanged so
the UI verdict is identical to the CLI's.
"""

from __future__ import annotations

from typing import Any

from ragmonk.cli.doctor import overall_status, run_checks
from ragmonk.core.lifecycle import AppContext


def health_report(ctx: AppContext) -> dict[str, Any]:
    sections = run_checks(ctx)
    return {
        "overall": overall_status(sections),
        "sections": [
            {
                "name": section.name,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail}
                    for c in section.checks
                ],
            }
            for section in sections
        ],
    }
