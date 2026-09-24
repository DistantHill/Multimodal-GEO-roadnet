"""In-memory closed XY grids for one Detect cross-track task."""

from __future__ import annotations

import math
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from threading import Lock, local
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from .candidate_patches import (
    CrossTrackCapacityError, build_cross_track_index, iter_cross_track_patches,
)
from .cross_track import intersect_cross_track_patch
from .db.grid_points import fetch_points_in_grid
from .logger import log_info
from .models import CrossTrackCandidate, PointRef, RefinedPointRef
from .projection import CoordinateTransformer


class GridCell(tuple[float, float, float, float]):
    """A four-bound cell that also carries its mutable task status."""

    status: str

    def __new__(cls, bounds: tuple[float, float, float, float]) -> GridCell:
        cell = super().__new__(cls, bounds)
        cell.status = "todo"
        return cell

    @property
    def aabb(self) -> tuple[float, float, float, float]:
        return tuple(self)


def create_grid(
    extent: tuple[float, float, float, float] | None,
    cell_size_m: float,
) -> list[list[GridCell]]:
    """Build ``grids[col][row]`` over Refine's processing-XY extent.

    Each cell has a closed AABB. Adjacent cells therefore share boundaries,
    and ``GridCell`` can be unpacked directly by ``fetch_points_in_grid``.
    An empty Refine extent gives an empty grid.
    """
    if not math.isfinite(cell_size_m) or cell_size_m <= 0:
        raise ValueError("cell_size_m must be finite and positive")
    if extent is None:
        return []
    x_min, y_min, x_max, y_max = extent
    if not all(math.isfinite(v) for v in extent):
        raise ValueError("extent must be finite")
    if x_min > x_max or y_min > y_max:
        raise ValueError("extent bounds are reversed")

    cols = max(1, math.ceil((x_max - x_min) / cell_size_m))
    rows = max(1, math.ceil((y_max - y_min) / cell_size_m))
    return [
        [
            GridCell((
                x_min + col * cell_size_m,
                y_min + row * cell_size_m,
                min(x_min + (col + 1) * cell_size_m, x_max),
                min(y_min + (row + 1) * cell_size_m, y_max),
            ))
            for row in range(rows)
        ]
        for col in range(cols)
    ]


class GridSegmentBudgetError(RuntimeError):
    """One grid exceeds its explicit segment count or memory budget."""


def build_grid_segments(
    point_batches: Iterable[Mapping[str, tuple[Any, ...]]],
    *,
    max_segments: int,
    max_bytes: int,
) -> dict[str, np.ndarray | tuple[Any, ...]]:
    """Deduplicate adjacent Refine segments from one grid's column batches.

    ``max_bytes`` bounds estimated retained Python records, identities,
    provenance strings, sorting references and final NumPy columns. It does
    not measure the input batch, database buffers, or whole-process RSS.
    Exceeding either budget raises ``GridSegmentBudgetError`` immediately.
    This function only constructs segments; it does not test intersections.
    """
    if max_segments < 0 or max_bytes < 0:
        raise ValueError("segment budgets must be non-negative")

    # Record layout: vehicle, subtrack, start sequence/kind/id, end kind/id,
    # end sequence, start source ID/key/reason, start timestamp/lon/lat,
    # start heading/speed, start/end XY, end heading.
    records: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    estimated_bytes = sys.getsizeof(records)
    iterator = iter(point_batches)
    try:
        for batch in iterator:
            count = len(batch["point_id"])
            if any(len(column) != count for column in batch.values()):
                raise ValueError("point batch columns have inconsistent lengths")
            for index in range(count):
                vehicle = batch["vehicle_id"][index]
                subtrack = int(batch["subtrack_index"][index])
                for start_prefix, end_prefix in (("prev_", ""), ("", "next_")):
                    start_id = batch[start_prefix + "point_id"][index]
                    end_id = batch[end_prefix + "point_id"][index]
                    if start_id is None or end_id is None:
                        continue
                    start_seq = int(batch[start_prefix + "refined_sequence_in_subtrack"][index])
                    end_seq = int(batch[end_prefix + "refined_sequence_in_subtrack"][index])
                    if end_seq != start_seq + 1:
                        raise ValueError("grid neighbours are not adjacent Refine points")
                    start_kind = int(batch[start_prefix + "point_kind"][index])
                    end_kind = int(batch[end_prefix + "point_kind"][index])
                    key = (vehicle, subtrack, start_kind, int(start_id),
                           end_kind, int(end_id))
                    if key in records:
                        continue
                    if len(records) >= max_segments:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_segments={max_segments}"
                        )
                    record = (
                        vehicle, subtrack, start_seq, start_kind, int(start_id),
                        end_kind, int(end_id), end_seq,
                        batch[start_prefix + "source_id"][index],
                        batch[start_prefix + "source_row_key"][index],
                        batch[start_prefix + "generated_reason"][index],
                        float(batch[start_prefix + "timestamp"][index]),
                        float(batch[start_prefix + "lon"][index]),
                        float(batch[start_prefix + "lat"][index]),
                        float(batch[start_prefix + "heading"][index]),
                        float(batch[start_prefix + "speed"][index]),
                        float(batch[start_prefix + "x"][index]),
                        float(batch[start_prefix + "y"][index]),
                        float(batch[end_prefix + "x"][index]),
                        float(batch[end_prefix + "y"][index]),
                        float(batch[end_prefix + "heading"][index]),
                    )
                    # Include key and record containers, unique field values,
                    # dictionary slack, two sorting lists, and 15 float/int
                    # NumPy output columns. This deliberately overestimates
                    # shared strings to make the payload budget conservative.
                    cost = (
                        sys.getsizeof(key) + sys.getsizeof(record)
                        + sum(sys.getsizeof(value) for value in record)
                        + 256 + 32 + 15 * 8
                    )
                    if estimated_bytes + cost > max_bytes:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_bytes={max_bytes}"
                        )
                    old_dict_bytes = sys.getsizeof(records)
                    records[key] = record
                    estimated_bytes += cost + max(
                        0, sys.getsizeof(records) - old_dict_bytes,
                    )
                    if estimated_bytes > max_bytes:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_bytes={max_bytes}"
                        )
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()

    ordered = [records[key] for key in sorted(
        records, key=lambda key: (
            key[0], key[1], records[key][2], key[2], key[3], key[4], key[5],
        ),
    )]
    length = len(ordered)
    floats = {
        name: np.fromiter((record[position] for record in ordered),
                          dtype=np.float64, count=length)
        for name, position in (
            ("timestamp", 11), ("lon", 12), ("lat", 13),
            ("heading", 14), ("speed", 15),
            ("x_start", 16), ("y_start", 17),
            ("x_end", 18), ("y_end", 19), ("end_heading", 20),
        )
    }
    integers = {
        name: np.fromiter((record[position] for record in ordered),
                          dtype=np.int64, count=length)
        for name, position in (
            ("subtrack_index", 1),
            ("start_refined_sequence_in_subtrack", 2),
            ("start_point_id", 4), ("end_point_id", 6),
            ("end_refined_sequence_in_subtrack", 7),
        )
    }
    return {
        **floats, **integers,
        "vehicle_id": tuple(record[0] for record in ordered),
        "start_point_kind": tuple(
            "kept" if record[3] == 0 else "injected" for record in ordered
        ),
        "end_point_kind": tuple(
            "kept" if record[5] == 0 else "injected" for record in ordered
        ),
        "start_source_id": tuple(record[8] for record in ordered),
        "start_source_row_key": tuple(record[9] for record in ordered),
        "start_generated_reason": tuple(record[10] for record in ordered),
        "segment_identity": tuple(
            (record[0], record[1], record[3], record[4], record[5], record[6])
            for record in ordered
        ),
    }


def iter_grid_crossings(
    segments: Mapping[str, np.ndarray | tuple[Any, ...]],
    cfg: Any,
    *,
    max_grid_memberships: int,
    max_overflow_pair_space: int,
) -> Iterator[dict[str, np.ndarray | tuple[Any, ...]]]:
    """Yield bounded exact cross-vehicle hits from one grid's segment columns.

    Straightness follows the existing Detect heading rule. The current
    spatial bucket index creates bounded pair batches; its capacity error is
    propagated unchanged so the caller can fail without retrying. Each yield
    contains at most ``cfg.cross_track_pair_batch_size`` exact hits and retains
    original segment column indices plus stable identities for later grid merge.
    """
    threshold = float(cfg.cross_track_straightness_deg)
    batch_size = int(cfg.cross_track_pair_batch_size)
    cell_m = float(cfg.cross_track_grid_cell_m)
    max_cells = int(cfg.cross_track_max_grid_cells_per_segment)
    if not math.isfinite(threshold) or not 0 <= threshold <= 180:
        raise ValueError("cross-track straightness threshold is invalid")
    if batch_size < 1 or not math.isfinite(cell_m) or cell_m <= 0 or max_cells < 1:
        raise ValueError("cross-track bucket parameters are invalid")
    if max_grid_memberships < 0 or max_overflow_pair_space < 0:
        raise ValueError("cross-track bucket budgets must be non-negative")

    vehicle_ids = segments["vehicle_id"]
    identities = segments["segment_identity"]
    size = len(vehicle_ids)
    required = (
        "x_start", "y_start", "x_end", "y_end", "heading", "end_heading",
    )
    if len(identities) != size or any(len(segments[name]) != size for name in required):
        raise ValueError("segment columns have inconsistent lengths")
    if size < 2:
        return
    heading = np.asarray(segments["heading"], dtype=np.float64)
    end_heading = np.asarray(segments["end_heading"], dtype=np.float64)
    delta = (end_heading - heading + 180.0) % 360.0 - 180.0
    retained = np.flatnonzero(np.abs(delta) <= threshold)
    if len(retained) < 2:
        return
    start = np.column_stack((
        np.asarray(segments["x_start"], dtype=np.float64)[retained],
        np.asarray(segments["y_start"], dtype=np.float64)[retained],
    ))
    end = np.column_stack((
        np.asarray(segments["x_end"], dtype=np.float64)[retained],
        np.asarray(segments["y_end"], dtype=np.float64)[retained],
    ))
    headings = heading[retained]
    if not (np.isfinite(start).all() and np.isfinite(end).all()
            and np.isfinite(headings).all()):
        raise ValueError("segment coordinates and headings must be finite")
    ranks = {vehicle: rank for rank, vehicle in enumerate(sorted(set(vehicle_ids)))}
    proxies = [
        SimpleNamespace(
            x1=float(start[index, 0]), y1=float(start[index, 1]),
            x2=float(end[index, 0]), y2=float(end[index, 1]),
            vehicle_rank=ranks[vehicle_ids[int(original)]],
        )
        for index, original in enumerate(retained)
    ]
    index = build_cross_track_index(
        proxies, cell_m=cell_m, max_cells=max_cells,
        max_grid_memberships=max_grid_memberships,
        max_overflow_pair_space=max_overflow_pair_space,
    )
    for ids_a, ids_b in iter_cross_track_patches(index, batch_size):
        xs, ys, hit_indices = intersect_cross_track_patch(
            start, end, headings, ids_a, ids_b, cfg,
        )
        if len(hit_indices) == 0:
            continue
        original_a = retained[ids_a[hit_indices]]
        original_b = retained[ids_b[hit_indices]]
        yield {
            "x": xs, "y": ys,
            "segment_index_a": original_a,
            "segment_index_b": original_b,
            "segment_identity_a": tuple(identities[int(i)] for i in original_a),
            "segment_identity_b": tuple(identities[int(i)] for i in original_b),
        }


def iter_grid_candidates(
    segments: Mapping[str, np.ndarray | tuple[Any, ...]],
    crossing_batches: Iterable[Mapping[str, np.ndarray | tuple[Any, ...]]],
    transformer: CoordinateTransformer,
) -> Iterator[tuple[tuple[Any, Any], CrossTrackCandidate]]:
    """Build candidate provenance for exact hits without numbering or merging.

    Each yield is ``((segment_identity_a, segment_identity_b), candidate)``.
    The pair key lets a later grid merger remove duplicate segment-pair hits.
    """
    identities = segments["segment_identity"]
    vehicle_ids = segments["vehicle_id"]
    for batch in crossing_batches:
        size = len(batch["x"])
        if any(len(batch[name]) != size for name in (
            "y", "segment_index_a", "segment_index_b",
            "segment_identity_a", "segment_identity_b",
        )):
            raise ValueError("crossing batch columns have inconsistent lengths")
        for hit_index in range(size):
            segment_a = int(batch["segment_index_a"][hit_index])
            segment_b = int(batch["segment_index_b"][hit_index])
            if not (0 <= segment_a < len(identities)
                    and 0 <= segment_b < len(identities)):
                raise IndexError("crossing segment index is outside columns")
            pair_identity = (identities[segment_a], identities[segment_b])
            if pair_identity != (
                batch["segment_identity_a"][hit_index],
                batch["segment_identity_b"][hit_index],
            ):
                raise ValueError("crossing segment identity differs from columns")
            if vehicle_ids[segment_a] >= vehicle_ids[segment_b]:
                raise ValueError("crossing segments must be in canonical vehicle order")

            refs = []
            refined_refs = []
            for segment_index in (segment_a, segment_b):
                kind = segments["start_point_kind"][segment_index]
                point_id = int(segments["start_point_id"][segment_index])
                source_id = segments["start_source_id"][segment_index]
                source_key = segments["start_source_row_key"][segment_index]
                reason = segments["start_generated_reason"][segment_index]
                if kind == "kept":
                    if not isinstance(source_id, str) or not isinstance(source_key, str):
                        raise ValueError("kept segment start has no source reference")
                    point_ref = PointRef(source_id, source_key, True)
                elif kind == "injected":
                    if not isinstance(reason, str) or not reason:
                        raise ValueError("injected segment start has no reason")
                    point_ref = PointRef(
                        "public.injected_points", str(point_id), False,
                        generated_reason=reason,
                    )
                else:
                    raise ValueError("segment start point_kind is invalid")
                refs.append(point_ref)
                refined_refs.append(RefinedPointRef(
                    kind, point_id,
                    int(segments["subtrack_index"][segment_index]),
                    int(segments["start_refined_sequence_in_subtrack"][segment_index]),
                ))
            x = float(batch["x"][hit_index])
            y = float(batch["y"][hit_index])
            lon, lat = transformer.inverse(x, y)
            candidate = CrossTrackCandidate(
                point_ref=refs[0],
                from_segment_start_point_ref=refs[0],
                to_segment_start_point_ref=refs[1],
                vehicle_id=f"{vehicle_ids[segment_a]}|{vehicle_ids[segment_b]}",
                timestamp=(float(segments["timestamp"][segment_a])
                           + float(segments["timestamp"][segment_b])) / 2.0,
                lon=lon, lat=lat, x=x, y=y,
                heading=float(segments["heading"][segment_a]),
                speed=float(segments["speed"][segment_a]),
                from_segment_start=refined_refs[0],
                to_segment_start=refined_refs[1],
            )
            yield pair_identity, candidate


def run_grid_cross_track_task(
    refine_id: Any,
    extent: tuple[float, float, float, float] | None,
    spatial_context: Any,
    transformer: CoordinateTransformer,
    cfg: Any,
    *,
    connect_worker: Callable[[], Any],
    grid_cell_size_m: float,
    max_segments: int,
    max_segment_bytes: int,
    max_grid_memberships: int,
    max_overflow_pair_space: int,
    max_candidates: int,
    max_result_bytes: int,
    thread_count: int = 4,
    candidate_store: Any = None,
    live_status: Any = None,
) -> Any:
    """Run only the grid cross-track task with bounded local threads/results.

    ``connect_worker`` creates one PostgreSQL connection per worker thread;
    this function closes every connection it creates. The shared result budget
    covers both in-flight worker candidates and deduplicated candidates held
    for final ordering. Budget errors and ``MemoryError`` fail immediately;
    other task errors receive at most two retries. No stage wiring occurs here.
    """
    if isinstance(thread_count, bool) or not 1 <= thread_count <= 8:
        raise ValueError("thread_count must be between 1 and 8")
    if max_candidates < 0 or max_result_bytes < 0:
        raise ValueError("candidate budgets must be non-negative")
    grids = create_grid(extent, grid_cell_size_m)
    pending = deque((col, row)
                    for col, column in enumerate(grids)
                    for row in range(len(column)))
    total = len(pending)
    started = time.monotonic()
    last_log_at = started
    log_info(f"Detect cross-track grid task: {total:,} grids")
    if live_status is not None:
        live_status.update(detail=f"跨轨迹网格：0/{total} 完成，0 线段，0 候选",
                           elapsed=0.0, stage="Detect: 跨轨迹网格")
    if total == 0:
        if candidate_store is not None:
            candidate_store.seal()
            return candidate_store
        return []

    thread_state = local()
    opened_connections: list[Any] = []
    connection_lock = Lock()
    budget_lock = Lock()
    result_count = 0
    result_bytes = 0
    attempts: dict[tuple[int, int], int] = {}
    merged: dict[tuple[Any, Any], CrossTrackCandidate] = {}
    done_count = 0
    segment_count = 0

    def worker(grid_index: tuple[int, int]) -> tuple[list[tuple[Any, CrossTrackCandidate, int]], int]:
        nonlocal result_count, result_bytes
        worker_conn = getattr(thread_state, "connection", None)
        if worker_conn is None or worker_conn.closed:
            worker_conn = connect_worker()
            thread_state.connection = worker_conn
            with connection_lock:
                opened_connections.append(worker_conn)
        reserved: list[tuple[Any, CrossTrackCandidate, int]] = []
        try:
            with worker_conn.transaction():
                batches = fetch_points_in_grid(
                    worker_conn, grid_index, grids, refine_id, spatial_context,
                )
                segments = build_grid_segments(
                    batches, max_segments=max_segments, max_bytes=max_segment_bytes,
                )
                crossings = iter_grid_crossings(
                    segments, cfg,
                    max_grid_memberships=max_grid_memberships,
                    max_overflow_pair_space=max_overflow_pair_space,
                )
                for pair, candidate in iter_grid_candidates(
                    segments, crossings, transformer,
                ):
                    # The estimate includes the candidate object graph, pair
                    # identity, dict/list references, and sorting references.
                    cost = (1024 + sys.getsizeof(candidate)
                            + sys.getsizeof(pair)
                            + sum(sys.getsizeof(item) for key in pair for item in key))
                    with budget_lock:
                        if (result_count + 1 > max_candidates
                                or result_bytes + cost > max_result_bytes):
                            raise GridSegmentBudgetError(
                                "cross-track candidate result budget exceeded"
                            )
                        result_count += 1
                        result_bytes += cost
                    reserved.append((pair, candidate, cost))
            return reserved, len(segments["segment_identity"])
        except Exception:
            with budget_lock:
                result_count -= len(reserved)
                result_bytes -= sum(item[2] for item in reserved)
            raise

    def progress() -> None:
        nonlocal last_log_at
        running = ", ".join(
            f"({col},{row})" for col, row in sorted(active.values())
        )
        now = time.monotonic()
        detail = (f"跨轨迹网格：{done_count}/{total} 完成，"
                  f"{segment_count} 线段，{len(merged)} 候选；"
                  f"运行中 {running or '无'}")
        if live_status is not None:
            live_status.update(
                detail=detail, elapsed=now - started,
                stage="Detect: 跨轨迹网格",
            )
        if now - last_log_at >= 3.0:
            log_info(f"{detail}；耗时 {now - started:.1f}s")
            last_log_at = now

    active: dict[Any, tuple[int, int]] = {}
    try:
        with ThreadPoolExecutor(max_workers=thread_count) as pool:
            while pending or active:
                while pending and len(active) < thread_count:
                    grid_index = pending.popleft()
                    grids[grid_index[0]][grid_index[1]].status = "running"
                    active[pool.submit(worker, grid_index)] = grid_index
                finished, _ = wait(active, timeout=3.0,
                                   return_when=FIRST_COMPLETED)
                if not finished:
                    progress()
                    continue
                for future in finished:
                    grid_index = active.pop(future)
                    cell = grids[grid_index[0]][grid_index[1]]
                    try:
                        results, grid_segments = future.result()
                    except Exception as exc:
                        cell.status = "failed"
                        if isinstance(exc, (
                            GridSegmentBudgetError, CrossTrackCapacityError,
                            MemoryError, ValueError, TypeError,
                        )):
                            raise
                        tried = attempts.get(grid_index, 0)
                        if tried >= 2:
                            raise
                        attempts[grid_index] = tried + 1
                        cell.status = "todo"
                        pending.appendleft(grid_index)
                        progress()
                        continue
                    with budget_lock:
                        for pair, candidate, cost in results:
                            if pair in merged:
                                result_count -= 1
                                result_bytes -= cost
                            else:
                                merged[pair] = candidate
                    cell.status = "done"
                    done_count += 1
                    segment_count += grid_segments
                    progress()
    finally:
        for worker_conn in opened_connections:
            worker_conn.close()

    if any(cell.status != "done" for column in grids for cell in column):
        raise RuntimeError("cross-track grid task ended before all grids were done")
    ordered = sorted(merged.items(), key=lambda item: (
        item[0][0][0], item[0][1][0],
        item[1].from_segment_start.subtrack_index,
        item[1].from_segment_start.refined_sequence_in_subtrack,
        item[1].to_segment_start.subtrack_index,
        item[1].to_segment_start.refined_sequence_in_subtrack,
        item[0],
    ))
    numbered = (
        replace(candidate, cross_track_calculate_index=index)
        for index, (_pair, candidate) in enumerate(ordered)
    )
    if candidate_store is not None:
        candidate_store.extend(numbered)
        candidate_store.seal()
        return candidate_store
    return list(numbered)
