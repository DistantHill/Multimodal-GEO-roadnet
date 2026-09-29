"""Insert parsed trajectory columns for the standalone CSV importer."""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..db.source_table import quote_table_name
from ..models import SourceBatch, _to_output_value
from ..telemetry.heartbeat import heartbeat_progress, operation_heartbeat
from .insert_batches import _copy_insert


_COLUMNS = ["vehicle_id", "timestamp", "lon", "lat", "heading", "speed", "extra"]


def _point_row(points: SourceBatch, index: int) -> tuple[Any, ...]:
    return (
        points.vehicle_id, float(points.timestamp[index]),
        float(points.lonlat[index, 0]), float(points.lonlat[index, 1]),
        float(points.heading[index]), float(points.speed[index]),
        json.dumps(
            {name: _to_output_value(values[index]) for name, values in points.extra.items()},
            ensure_ascii=False,
        ),
    )


def copy_trajectory_point_batches(
    conn: Any, batches: Iterable[SourceBatch], *, table: str = "trajectory_points",
) -> int:
    """Write a lazy group of vehicle batches through one COPY."""
    target = quote_table_name(table)
    count = 0
    with operation_heartbeat(f"写入 {target}"):
        with conn.cursor() as cursor:
            with cursor.copy(f"COPY {target} ({', '.join(_COLUMNS)}) FROM STDIN") as copy:
                for points in batches:
                    for index in range(len(points)):
                        copy.write_row(_point_row(points, index))
                        count += 1
                        if count % 10_000 == 0:
                            heartbeat_progress(count)
        heartbeat_progress(count)
    return count


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
    with conn.cursor() as cursor:
        for start in range(0, len(points), batch_size):
            stop = min(start + batch_size, len(points))
            rows = [_point_row(points, index) for index in range(start, stop)]
            _copy_insert(cursor, target, _COLUMNS, rows)
    return len(points)
