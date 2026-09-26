"""Columnar track grouping shared by map replay and Clip projections."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any
import numpy as np
from ..map_features import create_map_feature


@dataclass(frozen=True, slots=True)
class TrackBatch:
    lonlat: np.ndarray
    resolved_point_ids: np.ndarray
    resolved_valid: np.ndarray
    source_row_keys: np.ndarray
    source_valid: np.ndarray
    point_kinds: np.ndarray
    crossing_ids: np.ndarray
    intersection_ids: np.ndarray
    crossing_types: np.ndarray
    crossing_valid: np.ndarray
    intersection_valid: np.ndarray

    def __post_init__(self) -> None:
        n = len(self.lonlat)
        if self.lonlat.shape != (n, 2) or self.lonlat.dtype != np.float64:
            raise ValueError("lonlat must be float64 (N,2)")
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name != "lonlat" and value.shape != (n,):
                raise ValueError(f"{item.name} must have shape (N,)")
            if item.name.endswith("_valid") and value.dtype != np.bool_:
                raise ValueError(f"{item.name} must be boolean")
            if item.name in {"resolved_point_ids", "source_row_keys", "crossing_ids", "intersection_ids"} and value.dtype != np.int64:
                raise ValueError(f"{item.name} must be int64")
            value.flags.writeable = False

    def __len__(self) -> int:
        return len(self.lonlat)

    def slice(self, start: int, stop: int) -> TrackBatch:
        return TrackBatch(*(getattr(self, item.name)[start:stop] for item in fields(self)))


def group_track_rows(rows: list[Any]) -> dict[tuple[str, int], TrackBatch]:
    columns: dict[tuple[str, int], list[list]] = {}
    for row in rows:
        key = (str(row[1]), int(row[0]))
        target = columns.setdefault(key, [[] for _ in range(8)])
        for column, value in zip(target, row[2:], strict=True):
            column.append(value)
    grouped = {}
    for key, data in columns.items():
        def identities(index: int) -> tuple[np.ndarray, np.ndarray]:
            return (np.asarray([value if value is not None else 0 for value in data[index]], dtype=np.int64),
                    np.asarray([value is not None for value in data[index]], dtype=np.bool_))
        resolved, resolved_valid = identities(0)
        source, source_valid = identities(1)
        crossing, crossing_valid = identities(5)
        intersection, intersection_valid = identities(6)
        grouped[key] = TrackBatch(
            np.column_stack((data[2], data[3])).astype(np.float64), resolved, resolved_valid,
            source, source_valid, np.asarray(data[4]), crossing, intersection,
            np.asarray([value if value is not None else -1 for value in data[7]], dtype=np.int8),
            crossing_valid, intersection_valid,
        )
    return grouped


def complete_track_groups(rows: list[Any], max_points: int) -> tuple[dict[tuple[str, int], TrackBatch], bool]:
    truncated = len(rows) > max_points
    kept = rows[:max_points]
    if truncated and kept:
        last_key = (str(kept[-1][1]), int(kept[-1][0]))
        next_key = (str(rows[max_points][1]), int(rows[max_points][0]))
        if next_key == last_key:
            kept = [row for row in kept if (str(row[1]), int(row[0])) != last_key]
    return group_track_rows(kept), truncated


def build_track_feature(key: tuple[str, int], points: TrackBatch, detect_id: str | None) -> dict[str, Any]:
    coordinates = points.lonlat.tolist()
    geometry = {"type": "Point", "coordinates": coordinates[0]} if len(points) == 1 else {
        "type": "LineString", "coordinates": coordinates}
    return create_map_feature(
        f"{key[0]}:{key[1]}", geometry, vehicle_id=key[0], subtrack_index=key[1], detect_id=detect_id,
        resolved_point_ids=[int(value) if valid else None for value,valid in zip(points.resolved_point_ids, points.resolved_valid)],
        source_row_keys=[str(value) if valid else None for value,valid in zip(points.source_row_keys, points.source_valid)],
        point_kinds=points.point_kinds.tolist(),
        crossing_ids=[int(value) if valid else None for value,valid in zip(points.crossing_ids, points.crossing_valid)],
        point_count=len(points),
    )
