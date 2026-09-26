"""Progress reporting shared by Detect orchestration and topology extraction."""
from __future__ import annotations

from typing import Any


def report_detect_progress(
    live_status: Any,
    *,
    detail: str,
    progress: float | None,
    elapsed: float,
    sub_detail: str = "",
    stage: str | None = None,
) -> None:
    """Route Detect progress to its live status panel or console fallback."""
    if live_status is not None:
        live_status.update(
            progress=progress,
            detail=detail,
            sub_detail=sub_detail,
            elapsed=elapsed,
            stage=stage,
        )
    else:
        print(f"    ... {detail} ({elapsed:.1f}s)")
