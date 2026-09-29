"""Partition tracks and spatial segment pairs without running detection kernels."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np



@dataclass
class CrossTrackIndex:
    spans: np.ndarray
    finite: np.ndarray
    representable: np.ndarray
    subtrack_indices: np.ndarray
    grid: dict[tuple[int, int], list[int]]
    overflow_segment_ids: list[int]


def build_cross_track_index(
    starts: np.ndarray,
    ends: np.ndarray,
    subtrack_indices: np.ndarray,
    *,
    cell_m: float,
    max_cells: int,
    max_grid_memberships: int | None = None,
    max_overflow_pair_space: int | None = None,
    telemetry: dict[str, int | float] | None = None,
) -> CrossTrackIndex:
    """Bound grid memberships; stream overflow work regardless of total pairs."""
    starts = np.asarray(starts, dtype=np.float64)
    ends = np.asarray(ends, dtype=np.float64)
    subtrack_indices = np.asarray(subtrack_indices, dtype=np.int64)
    if starts.shape != ends.shape or starts.shape != (len(subtrack_indices), 2):
        raise ValueError("segment columns have inconsistent shapes")
    spans=np.zeros((len(starts),4),dtype=np.int64)
    finite=np.isfinite(starts).all(axis=1)&np.isfinite(ends).all(axis=1)
    representable=finite.copy()
    limits=np.iinfo(np.int64)
    for i in np.flatnonzero(finite):
        quotients=(min(starts[i,0],ends[i,0])/cell_m,max(starts[i,0],ends[i,0])/cell_m,min(starts[i,1],ends[i,1])/cell_m,max(starts[i,1],ends[i,1])/cell_m)
        if not all(math.isfinite(value) and limits.min<=math.floor(value)<=limits.max for value in quotients):
            representable[i]=False
        else:
            spans[i]=tuple(math.floor(value) for value in quotients)
    if max_grid_memberships is not None or max_overflow_pair_space is not None:
        indexed_memberships, overflow_count = _estimate_grid_capacity(
            spans, finite, representable, max_cells,
        )
        overflow_pair_space = overflow_count * len(starts)
        if telemetry is not None:
            telemetry["grid_memberships_estimate"] = indexed_memberships
            telemetry["overflow_segments"] = overflow_count
            telemetry["overflow_pair_space_estimate"] = overflow_pair_space
        if (
            max_grid_memberships is not None
            and indexed_memberships > max_grid_memberships
        ):
            # Avoid allocating the oversized membership graph. Every finite
            # segment participates in the same exact, bounded overflow stream.
            overflow_ids = np.flatnonzero(finite).tolist()
            if telemetry is not None:
                telemetry["grid_membership_fallback"] = 1
                telemetry["overflow_segments"] = len(overflow_ids)
                telemetry["overflow_pair_space_estimate"] = len(overflow_ids) ** 2
                telemetry["overflow_pair_budget_exceeded"] = int(
                    max_overflow_pair_space is not None
                    and len(overflow_ids) ** 2 > max_overflow_pair_space
                )
            return CrossTrackIndex(spans, finite, representable, subtrack_indices, {}, overflow_ids)
        # This is total work, not retained memory. The overflow iterator emits
        # bounded patches, so a large estimate must not reject a valid grid.
        if telemetry is not None:
            telemetry["overflow_pair_budget_exceeded"] = int(
                max_overflow_pair_space is not None
                and overflow_pair_space > max_overflow_pair_space
            )
    grid, overflow_ids = _build_grid(spans, finite, representable, max_cells)
    return CrossTrackIndex(
        spans,
        finite,
        representable,
        subtrack_indices,
        grid,
        overflow_ids,
    )


def _estimate_grid_capacity(spans,finite,representable,max_cells):
    memberships=overflow=0
    for i in np.flatnonzero(finite):
        if not representable[i]: overflow+=1; continue
        x0,x1,y0,y1=(int(value) for value in spans[i])
        count=(x1-x0+1)*(y1-y0+1)
        if count>max_cells: overflow+=1
        else: memberships+=count
    return memberships,overflow


def _build_grid(spans,finite,representable,max_cells):
    grid=defaultdict(list); overflow=[]
    for i in np.flatnonzero(finite):
        if not representable[i]: overflow.append(int(i)); continue
        x0,x1,y0,y1=(int(value) for value in spans[i])
        if (x1-x0+1)*(y1-y0+1)>max_cells: overflow.append(int(i)); continue
        for x in range(x0,x1+1):
            for y in range(y0,y1+1): grid[(x,y)].append(int(i))
    return grid,overflow


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


def _iter_cross_subtrack_pair_batches(
    segment_ids: list[int] | np.ndarray,
    subtrack_indices: np.ndarray,
    batch_size: int,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield only canonical cross-subtrack pairs from rank-ordered segments."""
    ids = np.asarray(segment_ids, dtype=np.int64)
    if len(ids) < 2:
        return
    ranks = subtrack_indices[ids]
    if np.any(ranks[1:] < ranks[:-1]):
        raise ValueError("cross-track segment ids must be ordered by subtrack index")

    batch_a = np.empty(batch_size, dtype=np.int64)
    batch_b = np.empty(batch_size, dtype=np.int64)
    pending = 0
    for position, segment_id in enumerate(ids):
        first_other_subtrack = int(np.searchsorted(ranks, ranks[position], side="right"))
        candidates = ids[first_other_subtrack:]
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


def iter_cross_track_filtered_patches(
    index: CrossTrackIndex, starts: np.ndarray, ends: np.ndarray, bearings: np.ndarray,
    cfg, *, telemetry: dict[str, int | float] | None = None,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Query interval overlaps inside existing buckets before gathering coordinates.

    For each bucket, sweep the axis with fewer overlapping intervals. Ragged
    interval ranges are decoded in bounded batches, never a bucket Cartesian
    matrix. Minimum shared cell ownership removes multi-cell duplicates.
    ``bearings`` contains endpoint directions shared with the exact kernel.
    """
    batch_size = int(cfg.cross_track_pair_batch_size)
    if batch_size < 1:
        raise ValueError("cross-track patch size must be positive")
    lower = np.minimum(starts, ends)
    upper = np.maximum(starts, ends)
    partitions = index.subtrack_indices
    pending_a = np.empty(batch_size, dtype=np.int64)
    pending_b = np.empty(batch_size, dtype=np.int64)
    pending = 0

    def filter_pairs(a, b, cell=None):
        if telemetry is not None:
            telemetry["bbox_query_pairs"] = telemetry.get("bbox_query_pairs", 0) + len(a)
        keep = partitions[a] != partitions[b]
        for axis in (0, 1):
            keep &= lower[a, axis] <= upper[b, axis]
            keep &= lower[b, axis] <= upper[a, axis]
        if cell is not None:
            keep &= np.maximum(index.spans[a, 0], index.spans[b, 0]) == cell[0]
            keep &= np.maximum(index.spans[a, 2], index.spans[b, 2]) == cell[1]
        a, b = a[keep], b[keep]
        swap = partitions[a] > partitions[b]
        a, b = np.where(swap, b, a), np.where(swap, a, b)
        # Use exactly the kernel's orientation and modulo arithmetic at boundaries.
        angle = np.abs((bearings[b] - bearings[a] + 180.0) % 360.0 - 180.0)
        keep = (cfg.cross_track_min_angle_deg <= angle) & (angle <= cfg.cross_track_max_angle_deg)
        return a[keep], b[keep]

    # Endpoint bearings are precomputed in [0, 360). Coarse direction buckets
    # are conservative; the exact angle check still decides every emitted pair.
    bearing_bin = np.floor((bearings % 360.0) / 10.0).astype(np.int64)

    def compatible(first, second):
        angle = abs(((second - first) * 10.0 + 180.0) % 360.0 - 180.0)
        return (angle + 10.000001 >= cfg.cross_track_min_angle_deg
                and angle - 10.000001 <= cfg.cross_track_max_angle_deg)

    def interval_pairs(left, right, *, same=False):
        # Disjoint direction groups query each other, never expand same-direction
        # combinations just to reject their angles afterwards.
        best = None
        for axis in (0, 1):
            a = left[np.argsort(lower[left, axis], kind="stable")]
            b = a if same else right[np.argsort(lower[right, axis], kind="stable")]
            first = (np.arange(len(a), dtype=np.int64) + 1 if same else
                     np.searchsorted(lower[b, axis], lower[a, axis], side="left"))
            stop = np.searchsorted(lower[b, axis], upper[a, axis], side="right")
            cumulative = np.cumsum(np.maximum(stop - first, 0))
            ranges = [(a, b, first, cumulative)]
            total = int(cumulative[-1]) if len(cumulative) else 0
            if not same:
                # Strict lower bound gives equal-minimum pairs to the first pass.
                first = np.searchsorted(lower[a, axis], lower[b, axis], side="right")
                stop = np.searchsorted(lower[a, axis], upper[b, axis], side="right")
                cumulative = np.cumsum(np.maximum(stop - first, 0))
                ranges.append((b, a, first, cumulative))
                total += int(cumulative[-1]) if len(cumulative) else 0
            if best is None or total < best[0]:
                best = (total, ranges)
        for a, b, first, cumulative in best[1]:
            offsets = np.concatenate(([0], cumulative[:-1]))
            total = int(cumulative[-1]) if len(cumulative) else 0
            for offset in range(0, total, batch_size):
                flat = np.arange(offset, min(offset + batch_size, total), dtype=np.int64)
                rows = np.searchsorted(cumulative, flat, side="right")
                other = first[rows] + flat - offsets[rows]
                yield a[rows], b[other]

    def candidates():
        for cell in sorted(index.grid):
            ids = np.asarray(index.grid[cell], dtype=np.int64)
            if len(ids) < 2:
                continue
            bins = np.unique(bearing_bin[ids])
            groups = {int(key): ids[bearing_bin[ids] == key] for key in bins}
            for position, first_bin in enumerate(bins):
                for second_bin in bins[position:]:
                    if not compatible(first_bin, second_bin):
                        continue
                    for a, b in interval_pairs(groups[first_bin], groups[second_bin],
                                               same=first_bin == second_bin):
                        yield filter_pairs(a, b, cell)

        # Long/unrepresentable segments are absent from buckets. Query sorted
        # minima, then check the other bounds in bounded vector batches. Only
        # overflow sources query this index; overflow/overflow pairs occur once.
        finite = np.flatnonzero(index.finite)
        if not index.overflow_segment_ids:
            return
        sorted_axes = [finite[np.argsort(lower[finite, axis], kind="stable")]
                       for axis in (0, 1)]
        sorted_minima = [lower[ids, axis] for axis, ids in enumerate(sorted_axes)]
        overflow = np.zeros(len(starts), dtype=np.bool_)
        overflow[index.overflow_segment_ids] = True
        for source in index.overflow_segment_ids:
            stops = [int(np.searchsorted(sorted_minima[axis], upper[source, axis], side="right"))
                     for axis in (0, 1)]
            axis = int(stops[1] < stops[0])
            ids = sorted_axes[axis]
            for offset in range(0, stops[axis], batch_size):
                b = ids[offset:min(offset + batch_size, stops[axis])]
                b = b[(b != source) & (~overflow[b] | (b > source))]
                yield filter_pairs(np.full(len(b), source, dtype=np.int64), b)

    # Coalesce sparse bucket results so the exact kernel processes full batches.
    for a, b in candidates():
        offset = 0
        while offset < len(a):
            take = min(batch_size - pending, len(a) - offset)
            pending_a[pending:pending + take] = a[offset:offset + take]
            pending_b[pending:pending + take] = b[offset:offset + take]
            pending += take
            offset += take
            if pending == batch_size:
                yield pending_a.copy(), pending_b.copy()
                pending = 0
    if pending:
        yield pending_a[:pending].copy(), pending_b[:pending].copy()
