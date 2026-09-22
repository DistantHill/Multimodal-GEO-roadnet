"""Cross-track segment preparation and exact detection for one pair patch.

Spatial indexing, patch scheduling and result aggregation live outside this
module. The historical implementations remain available as benchmark oracles.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from ._geometry import _angle_delta_deg, _paired_segment_intersections
from .models import CrossTrackCandidate, TrajectoryPoint
from .projection import CoordinateTransformer
from ._cross_track_legacy import (
    extract_cross_track_candidates_legacy,
    extract_cross_track_candidates_reference,
)


@dataclass(frozen=True)
class _CrossTrackSegment:
    """One source segment plus the data needed for bounded matching."""

    vehicle_id: str
    vehicle_rank: int
    track_rank: int
    segment_index: int
    start: TrajectoryPoint
    x1: float
    y1: float
    x2: float
    y2: float
    heading: float
    timestamp: float


def _prepare_segments(
    tracks: dict[str, list[TrajectoryPoint]],
    transformer: CoordinateTransformer,
    cfg: Any,
) -> tuple[list[_CrossTrackSegment], int]:
    """Preserve straight-segment filtering in stable vehicle order."""
    actual_vehicle_ids = sorted({
        track[0].vehicle_id for track in tracks.values() if track
    })
    vehicle_rank_by_id = {
        vehicle_id: rank for rank, vehicle_id in enumerate(actual_vehicle_ids)
    }
    per_track: list[
        tuple[str, str, int, int, list[tuple[int, float, float, float, float]]]
    ] = []
    for track_rank, track_key in enumerate(sorted(tracks)):
        track = tracks[track_key]
        if not track:
            continue
        vehicle_id = track[0].vehicle_id
        raw_segments: list[tuple[int, float, float, float, float]] = []
        projected_points: dict[int, tuple[float, float]] = {}

        def point_xy(point_index: int) -> tuple[float, float]:
            projected = projected_points.get(point_index)
            if projected is None:
                point = track[point_index]
                if (point.x is None) != (point.y is None):
                    raise ValueError("trajectory point must provide both processing x and y")
                projected = (
                    (float(point.x), float(point.y))
                    if point.x is not None
                    else transformer.forward(point.lon, point.lat)
                )
                projected_points[point_index] = projected
            return projected

        for segment_index in range(len(track) - 1):
            start = track[segment_index]
            end = track[segment_index + 1]
            if abs(_angle_delta_deg(start.heading, end.heading)) > cfg.cross_track_straightness_deg:
                continue
            x1, y1 = point_xy(segment_index)
            x2, y2 = point_xy(segment_index + 1)
            raw_segments.append((segment_index, x1, y1, x2, y2))
        if raw_segments:
            per_track.append((
                track_key,
                vehicle_id,
                vehicle_rank_by_id[vehicle_id],
                track_rank,
                raw_segments,
            ))

    segments: list[_CrossTrackSegment] = []
    for track_key, vehicle_id, vehicle_rank, track_rank, raw_segments in per_track:
        track = tracks[track_key]
        for segment_index, x1, y1, x2, y2 in raw_segments:
            start = track[segment_index]
            segments.append(_CrossTrackSegment(
                vehicle_id=vehicle_id,
                vehicle_rank=vehicle_rank,
                track_rank=track_rank,
                segment_index=segment_index,
                start=start,
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                heading=start.heading,
                timestamp=start.timestamp,
            ))
    return segments, len(actual_vehicle_ids)


def detect_cross_track_patch(
    segments: list[_CrossTrackSegment],
    starts: np.ndarray,
    ends: np.ndarray,
    headings: np.ndarray,
    segment_ids_a: np.ndarray,
    segment_ids_b: np.ndarray,
    cfg: Any,
    transformer: CoordinateTransformer,
) -> list[tuple[tuple[int, ...], CrossTrackCandidate]]:
    """Compute exact intersections for one externally scheduled segment-pair patch."""
    xs, ys, hit_indices = intersect_cross_track_patch(
        starts, ends, headings, segment_ids_a, segment_ids_b, cfg,
    )
    return list(iter_cross_track_patch_candidates(
        segments, segment_ids_a, segment_ids_b, xs, ys, hit_indices, transformer,
    ))


def intersect_cross_track_patch(
    starts: np.ndarray,
    ends: np.ndarray,
    headings: np.ndarray,
    segment_ids_a: np.ndarray,
    segment_ids_b: np.ndarray,
    cfg: Any,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run the exact numeric kernel without loading source point objects."""
    return _paired_segment_intersections(
        starts[segment_ids_a],
        ends[segment_ids_a],
        headings[segment_ids_a],
        starts[segment_ids_b],
        ends[segment_ids_b],
        headings[segment_ids_b],
        cfg.cross_track_min_angle_deg,
        cfg.cross_track_max_angle_deg,
    )


def build_cross_track_patch_candidates(
    segments: Sequence[_CrossTrackSegment] | Mapping[int, _CrossTrackSegment],
    segment_ids_a: np.ndarray,
    segment_ids_b: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    hit_indices: np.ndarray,
    transformer: CoordinateTransformer,
) -> list[tuple[tuple[int, ...], CrossTrackCandidate]]:
    """Build candidate provenance only for exact hits; this performs no I/O."""
    return list(iter_cross_track_patch_candidates(
        segments, segment_ids_a, segment_ids_b, xs, ys, hit_indices, transformer,
    ))


def iter_cross_track_patch_candidates(
    segments: Sequence[_CrossTrackSegment] | Mapping[int, _CrossTrackSegment],
    segment_ids_a: np.ndarray,
    segment_ids_b: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    hit_indices: np.ndarray,
    transformer: CoordinateTransformer,
) -> Iterator[tuple[tuple[int, ...], CrossTrackCandidate]]:
    """Yield candidate provenance in exact-kernel hit order without another list."""
    for x, y, hit_index in zip(xs, ys, hit_indices):
        segment_a = segments[int(segment_ids_a[hit_index])]
        segment_b = segments[int(segment_ids_b[hit_index])]
        yield (
            (
                segment_a.vehicle_rank,
                segment_b.vehicle_rank,
                segment_a.segment_index,
                segment_b.segment_index,
                segment_a.track_rank,
                segment_b.track_rank,
            ),
            _candidate_from_segments(
                segment_a,
                segment_b,
                float(x),
                float(y),
                transformer,
            ),
        )


def _candidate_from_segments(
    segment_a: _CrossTrackSegment,
    segment_b: _CrossTrackSegment,
    x: float,
    y: float,
    transformer: CoordinateTransformer,
) -> CrossTrackCandidate:
    """Build the public candidate payload from one canonical hit."""
    lon, lat = transformer.inverse(x, y)
    return CrossTrackCandidate(
        point_ref=segment_a.start.point_ref,
        from_segment_start_point_ref=segment_a.start.point_ref,
        to_segment_start_point_ref=segment_b.start.point_ref,
        vehicle_id=f"{segment_a.vehicle_id}|{segment_b.vehicle_id}",
        timestamp=(segment_a.timestamp + segment_b.timestamp) / 2.0,
        lon=lon,
        lat=lat,
        x=x,
        y=y,
        heading=segment_a.heading,
        speed=segment_a.start.speed,
        from_segment_start=segment_a.start.refined_point_ref,
        to_segment_start=segment_b.start.refined_point_ref,
    )
