"""Insert parsed trajectory points for the standalone trajectory importer."""
from __future__ import annotations

import json
from typing import Any

from ..db.source_table import quote_table_name
from ..models import TrajectoryPoint
from .insert_batches import _chunked_insert_returning

def insert_trajectory_points(
    conn: Any,
    points: list[TrajectoryPoint],
    *,
    table: str = "trajectory_points",
    batch_size: int,
) -> dict[tuple[str, str], int]:
    """Standalone CSV importer helper; Pipeline itself is table-only in v3."""
    if not points:
        return {}
    rows = [(
        point.vehicle_id,
        point.timestamp,
        point.lon,
        point.lat,
        point.heading,
        point.speed,
        json.dumps(point.extra, ensure_ascii=False),
    ) for point in points]
    with conn.cursor() as cur:
        returned = _chunked_insert_returning(
            cur,
            quote_table_name(table),
            ["vehicle_id", "timestamp", "lon", "lat", "heading", "speed", "extra"],
            rows,
            ["id"],
            batch_size=batch_size,
        )
    return {
        (point.point_ref.source_id, point.point_ref.source_row_key): int(row[0])
        for point, row in zip(points, returned, strict=True)
    }

