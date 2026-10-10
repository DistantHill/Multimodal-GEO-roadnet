"""Shared optional, timezone-aware Refine time selection."""
from __future__ import annotations

from datetime import datetime
import math
from typing import Any


TimeValue = datetime | str | None


def time_bounds(time_start: TimeValue, time_end: TimeValue) -> tuple[datetime | None, datetime | None]:
    from .experiments.config import parse_time
    start = None if time_start is None or time_start == "" else parse_time(time_start, "time_start")
    end = None if time_end is None or time_end == "" else parse_time(time_end, "time_end")
    if start is not None and end is not None and start >= end:
        raise ValueError("time_start must precede time_end")
    return start, end


def validate_refine_time(
    store: Any, record: Any, time_start: TimeValue = None, time_end: TimeValue = None,
) -> tuple[datetime | None, datetime | None]:
    """Check explicit endpoints against declared coverage or historical point bounds."""
    start, end = time_bounds(time_start, time_end)
    if start is None and end is None:
        return start, end
    selection = record.parameters.get("source_selection") or {}
    lower, upper = time_bounds(selection.get("time_start"), selection.get("time_end"))
    low = lower.timestamp() if lower is not None else None
    high = upper.timestamp() if upper is not None else None
    if low is None or high is None:
        from .db.source_table import validate_source_table
        contract = validate_source_table(store.connection, record.source_table)
        with store.connection.cursor() as cur:
            cur.execute(f"""SELECT min(timestamp), max(timestamp) FROM (
                SELECT s.timestamp FROM kept_trajectory_points k
                JOIN {contract.quoted_table} s ON s.id=k.source_row_key WHERE k.refine_id=%s
                UNION ALL SELECT timestamp FROM injected_points WHERE refine_id=%s
            ) points""", (record.id, record.id))
            observed_low, observed_high = cur.fetchone()
        if observed_low is None:
            raise ValueError(f"Refine Run {record.id}: cannot select time from an empty result")
        low = float(observed_low) if low is None else low
        high = math.nextafter(float(observed_high), math.inf) if high is None else high
    if ((start is not None and not low <= start.timestamp() < high)
            or (end is not None and not low < end.timestamp() <= high)):
        raise ValueError(f"Refine Run {record.id}: requested time range exceeds Refine time range")
    return start, end
