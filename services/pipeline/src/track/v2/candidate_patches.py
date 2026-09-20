"""Partition tracks and spatial segment pairs without running detection kernels."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ._perf import PerfTracker
from .models import TrajectoryPoint

if TYPE_CHECKING:
    from .cross_track import _CrossTrackSegment

CellSpan = tuple[int, int, int, int]
TrackPatch = dict[str, list[TrajectoryPoint]]


class CrossTrackCapacityError(RuntimeError):
    """The requested global broad-phase would exceed its memory/work budget."""


CROSS_TRACK_OVERFLOW_PAIR_LIMIT = 20_000_000


def iter_track_patches(
    tracks: Iterable[tuple[str, list[TrajectoryPoint]]],
    batch_size: int,
) -> Iterator[TrackPatch]:
    """Keep each track intact; emit bounded patches, including the final tail.

    Keys identify complete tracks/subtracks and must be unique in the input.
    The caller owns the input iterator's lifetime.
    """
    if batch_size <= 0:
        raise ValueError("track patch size must be positive")
    patch: TrackPatch = {}
    for key, track in tracks:
        patch[key] = track
        if len(patch) >= batch_size:
            yield patch
            patch = {}
    if patch:
        yield patch


@dataclass
class CrossTrackIndex:
    spans: list[CellSpan | None]
    vehicle_ranks: np.ndarray
    grid: dict[tuple[int, int], list[int]]
    overflow_segment_ids: list[int]


def build_cross_track_index(
    segments: list[_CrossTrackSegment],
    *,
    cell_m: float,
    max_cells: int,
    max_grid_memberships: int | None = None,
    max_overflow_pair_space: int | None = None,
    telemetry: dict[str, int | float] | None = None,
) -> CrossTrackIndex:
    """Check capacity before allocating grid memberships, then build the index."""
    spans = [_grid_span(s.x1, s.y1, s.x2, s.y2, cell_m) for s in segments]
    if max_grid_memberships is not None or max_overflow_pair_space is not None:
        indexed_memberships, overflow_count = _estimate_grid_capacity(
            spans, max_cells,
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
    grid, overflow_ids = _build_grid(spans, max_cells)
    return CrossTrackIndex(
        spans,
        np.array([s.vehicle_rank for s in segments], dtype=np.int64),
        grid,
        overflow_ids,
    )


def iter_cross_track_patches(
    index: CrossTrackIndex,
    batch_size: int,
    *,
    perf: PerfTracker | None = None,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield canonical cross-vehicle pairs once, bounded by the patch size.

    Multi-cell pairs belong to their minimum shared cell. Long segments use
    the same bounded pair stream, with overflow/overflow duplicates removed.
    """
    if batch_size <= 0:
        raise ValueError("cross-track patch size must be positive")
    min_cx = np.array([s[0] if s is not None else -1 for s in index.spans], dtype=np.int64)
    min_cy = np.array([s[2] if s is not None else -1 for s in index.spans], dtype=np.int64)
    cells = sorted(index.grid)
    cell_iter = perf.timed_iter(cells, name="cross_track_grid_cells", total=len(cells)) if perf else cells
    for cell_x, cell_y in cell_iter:
        seg_ids = np.asarray(index.grid[(cell_x, cell_y)], dtype=np.int64)
        if len(seg_ids) < 2:
            continue
        for ids_a, ids_b in _iter_cross_vehicle_pair_batches(seg_ids, index.vehicle_ranks, batch_size):
            keep = np.maximum(min_cx[ids_a], min_cx[ids_b]) == cell_x
            keep &= np.maximum(min_cy[ids_a], min_cy[ids_b]) == cell_y
            if np.any(keep):
                yield ids_a[keep], ids_b[keep]
    yield from _iter_overflow_pair_batches(
        index.spans, index.vehicle_ranks, index.overflow_segment_ids, batch_size,
    )


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


def _estimate_grid_capacity(
    spans: list[CellSpan | None],
    max_cells: int,
) -> tuple[int, int]:
    """Return indexed memberships and overflow count without allocating a grid."""
    memberships = 0
    overflow_count = 0
    for span in spans:
        if span is None:
            continue
        cell_min_x, cell_max_x, cell_min_y, cell_max_y = span
        cell_count = (cell_max_x - cell_min_x + 1) * (cell_max_y - cell_min_y + 1)
        if cell_count > max_cells:
            overflow_count += 1
        else:
            memberships += cell_count
    return memberships, overflow_count


def _build_grid(
    spans: list[CellSpan | None],
    max_cells: int,
) -> tuple[dict[tuple[int, int], list[int]], list[int]]:
    """Assign bounded AABBs to cells and retain long spans for exact fallback."""
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    overflow_segment_ids: list[int] = []
    for segment_id, span in enumerate(spans):
        if span is None:
            continue
        cell_min_x, cell_max_x, cell_min_y, cell_max_y = span
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
    spans: list[CellSpan | None],
    vehicle_ranks: np.ndarray,
    overflow_segment_ids: list[int],
    batch_size: int,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield every long-AABB pair once under the standard batch bound."""
    finite_segment_ids = [
        segment_id for segment_id, span in enumerate(spans)
        if span is not None
    ]
    overflow_ids = set(overflow_segment_ids)
    a_ids: list[int] = []
    b_ids: list[int] = []
    for overflow_id in overflow_segment_ids:
        overflow_rank = vehicle_ranks[overflow_id]
        for other_id in finite_segment_ids:
            if other_id == overflow_id or vehicle_ranks[other_id] == overflow_rank:
                continue
            if other_id in overflow_ids and other_id < overflow_id:
                continue
            other_rank = vehicle_ranks[other_id]
            if overflow_rank < other_rank:
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
