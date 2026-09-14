"""Method 3 cross-track candidate extraction.

The module owns segment preparation, spatial indexing, bounded exact matching,
candidate provenance, deterministic ordering, progress, and benchmark oracles.
Callers only need :func:`extract_cross_track_candidates`.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Iterator

import numpy as np

from ._geometry import (
    _angle_delta_deg,
    _paired_segment_intersections,
)
from ._perf import PerfTracker
from .models import CrossTrackCandidate, TrajectoryPoint
from .projection import CoordinateTransformer
from ._cross_track_legacy import (
    extract_cross_track_candidates_legacy,
    extract_cross_track_candidates_reference,
)


class CrossTrackCapacityError(RuntimeError):
    """The requested global broad-phase would exceed its memory/work budget."""


@dataclass(frozen=True)
class _CrossTrackSegment:
    """One source segment plus the data needed for bounded matching."""

    vehicle_id: str
    vehicle_rank: int
    track_rank: int
    segment_index: int
    start: TrajectoryPoint
    end: TrajectoryPoint
    x1: float
    y1: float
    x2: float
    y2: float
    heading: float
    timestamp: float
    cell_span: tuple[int, int, int, int] | None


def extract_cross_track_candidates(
    tracks: dict[str, list[TrajectoryPoint]],
    transformer: CoordinateTransformer,
    cfg: Any,
    live_status: Any = None,
    *,
    sub_detail: bool = False,
    perf: PerfTracker | None = None,
    telemetry: dict[str, int | float] | None = None,
    max_grid_memberships: int | None = None,
    max_overflow_pair_space: int | None = None,
) -> tuple[list[CrossTrackCandidate], int, int]:
    """Return cross-track candidates in stable vehicle/segment order.

    The optional budgets are used by DB mode before building a global index.
    They deliberately default to ``None`` so handle mode and the historical
    bounded-overflow behaviour remain unchanged.
    """
    extraction_started = time.perf_counter() if telemetry is not None else 0.0
    segment_started = time.perf_counter() if telemetry is not None else 0.0
    segments, vehicle_count = _prepare_segments(tracks, transformer, cfg)
    if telemetry is not None:
        telemetry["segment_build_seconds"] = telemetry.get(
            "segment_build_seconds", 0.0,
        ) + (time.perf_counter() - segment_started)
        telemetry["segments"] = telemetry.get("segments", 0) + len(segments)
    if vehicle_count < 2:
        return [], 0, 0

    total_pairs = vehicle_count * (vehicle_count - 1) // 2
    if max_grid_memberships is not None or max_overflow_pair_space is not None:
        indexed_memberships, overflow_count = _estimate_grid_capacity(
            segments,
            cfg.cross_track_max_grid_cells_per_segment,
        )
        overflow_pair_space = overflow_count * len(segments)
        if telemetry is not None:
            telemetry["grid_memberships_estimate"] = indexed_memberships
            telemetry["overflow_segments"] = overflow_count
            telemetry["overflow_pair_space_estimate"] = overflow_pair_space
        if (
            max_grid_memberships is not None
            and indexed_memberships > max_grid_memberships
        ):
            raise CrossTrackCapacityError(
                "global cross-track grid requires "
                f"{indexed_memberships:,} memberships; "
                f"budget is {max_grid_memberships:,}"
            )
        if (
            max_overflow_pair_space is not None
            and overflow_pair_space > max_overflow_pair_space
        ):
            raise CrossTrackCapacityError(
                "global cross-track overflow fallback spans "
                f"{overflow_pair_space:,} segment pairs; "
                f"budget is {max_overflow_pair_space:,}"
            )
    array_started = time.perf_counter() if telemetry is not None else 0.0
    starts = np.array([(segment.x1, segment.y1) for segment in segments], dtype=float)
    ends = np.array([(segment.x2, segment.y2) for segment in segments], dtype=float)
    headings = np.array([segment.heading for segment in segments], dtype=float)
    grid, overflow_segment_ids = _build_grid(segments, cfg.cross_track_max_grid_cells_per_segment)
    if telemetry is not None:
        telemetry["array_build_seconds"] = telemetry.get(
            "array_build_seconds", 0.0,
        ) + (time.perf_counter() - array_started)
        telemetry["vehicle_pairs"] = telemetry.get("vehicle_pairs", 0) + total_pairs

    ordered_candidates: list[tuple[tuple[int, ...], CrossTrackCandidate]] = []
    started = time.perf_counter()
    # ZH: 预计算每段的 vehicle_rank 与 AABB min 角 cell，供 owner 向量化过滤。
    # EN: Precompute per-segment vehicle_rank and AABB min-corner cell for
    #     vectorized owner filtering.
    ranks = np.array([segment.vehicle_rank for segment in segments], dtype=np.int64)
    min_cx = np.array(
        [segment.cell_span[0] if segment.cell_span is not None else -1 for segment in segments],
        dtype=np.int64,
    )
    min_cy = np.array(
        [segment.cell_span[2] if segment.cell_span is not None else -1 for segment in segments],
        dtype=np.int64,
    )
    cells = sorted(grid)
    cell_iter = perf.timed_iter(cells, name="cross_track_grid_cells", total=len(cells)) if perf else cells
    for cell_x, cell_y in cell_iter:
        seg_ids = np.asarray(grid[(cell_x, cell_y)], dtype=np.int64)
        if len(seg_ids) < 2:
            continue
        for segment_ids_a, segment_ids_b in _iter_cross_vehicle_pair_batches(
            seg_ids,
            ranks,
            cfg.cross_track_pair_batch_size,
        ):
            # Same-vehicle and reversed pairs were never materialized; this
            # mask only assigns a multi-cell pair to its canonical owner cell.
            keep = np.maximum(min_cx[segment_ids_a], min_cx[segment_ids_b]) == cell_x
            keep &= np.maximum(min_cy[segment_ids_a], min_cy[segment_ids_b]) == cell_y
            segment_ids_a = segment_ids_a[keep]
            segment_ids_b = segment_ids_b[keep]
            if len(segment_ids_a) == 0:
                continue
            _append_pair_hits(
                ordered_candidates,
                segments,
                starts,
                ends,
                headings,
                segment_ids_a,
                segment_ids_b,
                cfg,
                transformer,
            )

    for segment_ids_a, segment_ids_b in _iter_overflow_pair_batches(
        segments, overflow_segment_ids, cfg.cross_track_pair_batch_size,
    ):
        _append_pair_hits(
            ordered_candidates,
            segments,
            starts,
            ends,
            headings,
            segment_ids_a,
            segment_ids_b,
            cfg,
            transformer,
        )

    ordered_candidates.sort(key=lambda item: item[0])
    candidates = [
        replace(candidate, cross_track_calculate_index=index)
        for index, (_key, candidate) in enumerate(ordered_candidates)
    ]
    elapsed = time.perf_counter() - started
    _emit_progress(
        live_status,
        total_pairs,
        total_pairs,
        len(candidates),
        elapsed,
        sub_detail=sub_detail,
        final=True,
    )
    if telemetry is not None:
        telemetry["cross_track_seconds"] = telemetry.get(
            "cross_track_seconds", 0.0,
        ) + (time.perf_counter() - extraction_started)
        telemetry["output_candidates"] = telemetry.get("output_candidates", 0) + len(candidates)
    return candidates, total_pairs, total_pairs


def _estimate_grid_capacity(
    segments: list[_CrossTrackSegment],
    max_cells: int,
) -> tuple[int, int]:
    """Return indexed memberships and overflow count without allocating a grid."""
    memberships = 0
    overflow_count = 0
    for segment in segments:
        if segment.cell_span is None:
            continue
        cell_min_x, cell_max_x, cell_min_y, cell_max_y = segment.cell_span
        cell_count = (cell_max_x - cell_min_x + 1) * (cell_max_y - cell_min_y + 1)
        if cell_count > max_cells:
            overflow_count += 1
        else:
            memberships += cell_count
    return memberships, overflow_count


def _append_pair_hits(
    ordered_candidates: list[tuple[tuple[int, ...], CrossTrackCandidate]],
    segments: list[_CrossTrackSegment],
    starts: np.ndarray,
    ends: np.ndarray,
    headings: np.ndarray,
    segment_ids_a: np.ndarray,
    segment_ids_b: np.ndarray,
    cfg: Any,
    transformer: CoordinateTransformer,
) -> None:
    xs, ys, hit_indices = _paired_segment_intersections(
        starts[segment_ids_a],
        ends[segment_ids_a],
        headings[segment_ids_a],
        starts[segment_ids_b],
        ends[segment_ids_b],
        headings[segment_ids_b],
        cfg.cross_track_min_angle_deg,
        cfg.cross_track_max_angle_deg,
    )
    for x, y, hit_index in zip(xs, ys, hit_indices):
        segment_a = segments[int(segment_ids_a[hit_index])]
        segment_b = segments[int(segment_ids_b[hit_index])]
        ordered_candidates.append((
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
        ))


def _grid_span(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    cell_m: float,
) -> tuple[int, int, int, int] | None:
    """Return a closed-cell span; non-finite segments retain miss semantics."""
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        return None
    return (
        math.floor(min(x1, x2) / cell_m),
        math.floor(max(x1, x2) / cell_m),
        math.floor(min(y1, y2) / cell_m),
        math.floor(max(y1, y2) / cell_m),
    )


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
                end=track[segment_index + 1],
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                heading=start.heading,
                timestamp=start.timestamp,
                cell_span=_grid_span(x1, y1, x2, y2, cfg.cross_track_grid_cell_m),
            ))
    return segments, len(actual_vehicle_ids)


def _build_grid(
    segments: list[_CrossTrackSegment],
    max_cells: int,
) -> tuple[dict[tuple[int, int], list[int]], list[int]]:
    """Assign bounded AABBs to cells and retain long spans for exact fallback."""
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    overflow_segment_ids: list[int] = []
    for segment_id, segment in enumerate(segments):
        if segment.cell_span is None:
            continue
        cell_min_x, cell_max_x, cell_min_y, cell_max_y = segment.cell_span
        cell_count = (cell_max_x - cell_min_x + 1) * (cell_max_y - cell_min_y + 1)
        if cell_count > max_cells:
            overflow_segment_ids.append(segment_id)
            continue
        for cell_x in range(cell_min_x, cell_max_x + 1):
            for cell_y in range(cell_min_y, cell_max_y + 1):
                grid[(cell_x, cell_y)].append(segment_id)
    return grid, overflow_segment_ids


def _iter_pair_batches(
    segment_ids_a: list[int] | np.ndarray,
    segment_ids_b: list[int] | np.ndarray,
    batch_size: int,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield a Cartesian product in bounded index batches."""
    a_ids = np.asarray(segment_ids_a, dtype=int)
    b_ids = np.asarray(segment_ids_b, dtype=int)
    total = len(a_ids) * len(b_ids)
    for offset in range(0, total, batch_size):
        flat = np.arange(
            offset,
            min(offset + batch_size, total),
            dtype=np.int64,
        )
        yield a_ids[flat // len(b_ids)], b_ids[flat % len(b_ids)]


def _iter_cross_vehicle_pair_batches(
    segment_ids: list[int] | np.ndarray,
    vehicle_ranks: np.ndarray,
    batch_size: int,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield only canonical cross-vehicle pairs from rank-ordered segments."""
    ids = np.asarray(segment_ids, dtype=np.int64)
    if len(ids) < 2:
        return
    ranks = vehicle_ranks[ids]
    if np.any(ranks[1:] < ranks[:-1]):
        raise ValueError("cross-track segment ids must be ordered by vehicle rank")

    batch_a = np.empty(batch_size, dtype=np.int64)
    batch_b = np.empty(batch_size, dtype=np.int64)
    pending = 0
    for position, segment_id in enumerate(ids):
        first_other_vehicle = int(np.searchsorted(ranks, ranks[position], side="right"))
        candidates = ids[first_other_vehicle:]
        candidate_offset = 0
        while candidate_offset < len(candidates):
            take = min(batch_size - pending, len(candidates) - candidate_offset)
            batch_a[pending : pending + take] = segment_id
            batch_b[pending : pending + take] = candidates[
                candidate_offset : candidate_offset + take
            ]
            pending += take
            candidate_offset += take
            if pending == batch_size:
                yield batch_a.copy(), batch_b.copy()
                pending = 0
    if pending:
        yield batch_a[:pending].copy(), batch_b[:pending].copy()


def _iter_overflow_pair_batches(
    segments: list[_CrossTrackSegment],
    overflow_segment_ids: list[int],
    batch_size: int,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield every long-AABB pair once under the standard batch bound."""
    finite_segment_ids = [
        segment_id for segment_id, segment in enumerate(segments)
        if segment.cell_span is not None
    ]
    overflow_ids = set(overflow_segment_ids)
    a_ids: list[int] = []
    b_ids: list[int] = []
    for overflow_id in overflow_segment_ids:
        overflow = segments[overflow_id]
        for other_id in finite_segment_ids:
            if other_id == overflow_id or segments[other_id].vehicle_rank == overflow.vehicle_rank:
                continue
            if other_id in overflow_ids and other_id < overflow_id:
                continue
            other = segments[other_id]
            if overflow.vehicle_rank < other.vehicle_rank:
                a_ids.append(overflow_id)
                b_ids.append(other_id)
            else:
                a_ids.append(other_id)
                b_ids.append(overflow_id)
            if len(a_ids) == batch_size:
                yield np.asarray(a_ids, dtype=int), np.asarray(b_ids, dtype=int)
                a_ids.clear()
                b_ids.clear()
    if a_ids:
        yield np.asarray(a_ids, dtype=int), np.asarray(b_ids, dtype=int)


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


def _emit_progress(
    live_status: Any,
    pair_count: int,
    total_pairs: int,
    candidate_count: int,
    elapsed: float,
    *,
    sub_detail: bool = False,
    final: bool = False,
) -> str:
    """Emit the single cross-track progress format."""
    detail = (
        f"跨车求交/Cross-track:\t{pair_count:,}/{total_pairs:,}\t车辆对/pairs,\t"
        f"{candidate_count:,}\t候选/candidates"
    )
    line = detail if final else f"计算中：{detail}"
    if live_status is not None:
        if sub_detail:
            live_status.update(sub_detail=line, elapsed=elapsed)
        else:
            live_status.update(
                progress=(pair_count / total_pairs) if total_pairs else 0.0,
                detail=line,
                elapsed=elapsed,
            )
    else:
        print(f"    ... {line} ({elapsed:.1f}s)")
    return detail
