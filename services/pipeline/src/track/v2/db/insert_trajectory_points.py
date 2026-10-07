"""Insert parsed trajectory columns for the standalone CSV importer."""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..db.source_table import quote_table_name
from ..models import SourceBatch, _to_output_value
from ..telemetry.heartbeat import heartbeat_progress, operation_heartbeat
from .insert_batches import _copy_insert


_COLUMNS = ["vehicle_id", "timestamp", "lon", "lat", "heading", "speed", "extra"]


def _point_row(points: SourceBatch, index: int, *, include_extra: bool = True) -> tuple[Any, ...]:
    values = (
        points.vehicle_id, float(points.timestamp[index]),
        float(points.lonlat[index, 0]), float(points.lonlat[index, 1]),
        float(points.heading[index]), float(points.speed[index]),
    )
    if not include_extra:
        return values
    extra = json.dumps(
        {name: _to_output_value(column[index]) for name, column in points.extra.items()},
        ensure_ascii=False,
    )
    return (*values, extra)


def copy_trajectory_point_batches(
    conn: Any, batches: Iterable[SourceBatch], *, table: str = "trajectory_points",
    include_extra: bool = True,
) -> int:
    """Write a lazy group of vehicle batches through one COPY."""
    target = quote_table_name(table)
    columns = _COLUMNS if include_extra else _COLUMNS[:-1]
    count = 0
    with operation_heartbeat(f"写入 {target}"):
        with conn.cursor() as cursor:
            with cursor.copy(f"COPY {target} ({', '.join(columns)}) FROM STDIN") as copy:
                for points in batches:
                    for index in range(len(points)):
                        copy.write_row(_point_row(points, index, include_extra=include_extra))
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
