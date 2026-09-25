"""Track point grouping shared by map replay and Clip projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..map_features import create_map_feature


@dataclass(frozen=True, slots=True)
class TrackPoint:
    resolved_point_id: int | None
    source_row_key: str | None
    lon: float
    lat: float
    point_kind: str
    crossing_id: int | None = None
    intersection_id: int | None = None
    crossing_type: int | None = None

def group_track_rows(rows: list[Any]) -> dict[tuple[str, int], list[TrackPoint]]:
    grouped: dict[tuple[str, int], list[TrackPoint]] = {}
    for row in rows:
        key = (str(row[1]), int(row[0]))
        grouped.setdefault(key, []).append(
            TrackPoint(
                resolved_point_id=int(row[2]) if row[2] is not None else None,
                source_row_key=str(row[3]) if row[3] is not None else None,
                lon=float(row[4]),
                lat=float(row[5]),
                point_kind=str(row[6]),
                crossing_id=int(row[7]) if row[7] is not None else None,
                intersection_id=int(row[8]) if row[8] is not None else None,
                crossing_type=int(row[9]) if row[9] is not None else None,
            )
        )
    return grouped

def complete_track_groups(
    rows: list[Any],
    max_points: int,
) -> tuple[dict[tuple[str, int], list[TrackPoint]], bool]:
    truncated = len(rows) > max_points
    kept = rows[:max_points]
    if truncated and kept:
        last_key = (str(kept[-1][1]), int(kept[-1][0]))
        next_key = (str(rows[max_points][1]), int(rows[max_points][0]))
        if next_key == last_key:
            kept = [
                row
                for row in kept
                if (str(row[1]), int(row[0])) != last_key
            ]
    return group_track_rows(kept), truncated

def build_track_feature(
    key: tuple[str, int],
    points: list[TrackPoint],
    detect_id: str | None,
) -> dict[str, Any]:
    coordinates = [[point.lon, point.lat] for point in points]
    geometry: dict[str, Any]
    if len(coordinates) == 1:
        geometry = {"type": "Point", "coordinates": coordinates[0]}
    else:
        geometry = {"type": "LineString", "coordinates": coordinates}
    return create_map_feature(
        f"{key[0]}:{key[1]}",
        geometry,
        vehicle_id=key[0],
        subtrack_index=key[1],
        detect_id=detect_id,
        resolved_point_ids=[point.resolved_point_id for point in points],
        source_row_keys=[point.source_row_key for point in points],
        point_kinds=[point.point_kind for point in points],
        crossing_ids=[point.crossing_id for point in points],
        point_count=len(points),
    )
