"""Insert parsed trajectory columns for the standalone CSV importer."""
from __future__ import annotations

import json
from typing import Any

from ..db.source_table import quote_table_name
from ..models import SourceBatch, _to_output_value
from .insert_batches import _copy_insert


def insert_trajectory_points(
    conn: Any,
    points: SourceBatch,
    *,
    table: str = "trajectory_points",
    batch_size: int,
) -> int:
    """Write bounded database rows and return the number written."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not len(points):
        return 0
    target = quote_table_name(table)
    columns = ["vehicle_id", "timestamp", "lon", "lat", "heading", "speed", "extra"]
    with conn.cursor() as cursor:
        for start in range(0, len(points), batch_size):
            stop = min(start + batch_size, len(points))
            rows = [
                (points.vehicle_id, float(points.timestamp[index]),
                 float(points.lonlat[index, 0]), float(points.lonlat[index, 1]),
                 float(points.heading[index]), float(points.speed[index]),
                 json.dumps({name: _to_output_value(values[index])
                             for name, values in points.extra.items()}, ensure_ascii=False))
                for index in range(start, stop)
            ]
            _copy_insert(cursor, target, columns, rows)
    return len(points)
