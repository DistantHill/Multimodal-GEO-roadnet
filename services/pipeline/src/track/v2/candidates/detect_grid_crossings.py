"""Detect and schedule exact cross-track candidates within XY grids."""
from __future__ import annotations

import math
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from threading import Lock, local
from typing import Any, Callable

import numpy as np

from ..db.fetch_refine_points_in_grid import fetch_points_in_grid
from ..models import CrossCandidateBatch
from ..projection import CoordinateTransformer
from ..telemetry.console_logging import log_info
from .build_patches import (
    CrossTrackCapacityError, build_cross_track_index, iter_cross_track_patches,
)
from .detect_cross_track_pairs import (intersect_cross_track_patch, build_cross_track_patch_candidates, concatenate_cross_batches)
from .grid import create_grid
from .grid_segments import GridSegmentBudgetError, build_grid_segments


def iter_grid_crossings(
    segments: Mapping[str, np.ndarray | tuple[Any, ...]],
    cfg: Any,
    *,
    max_grid_memberships: int,
    max_overflow_pair_space: int,
) -> Iterator[dict[str, np.ndarray | tuple[Any, ...]]]:
    """Yield bounded exact cross-vehicle hits from one grid's segment columns.

    Straightness follows the existing Detect heading rule. The current
    spatial bucket index creates bounded pair batches; oversized membership
    graphs use the exact overflow stream instead of rejecting total work. Each yield
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
    partitions = np.asarray(segments["subtrack_index"], dtype=np.int64)[retained]
    # Pair ordering follows the Run-scoped subtrack identity, independently of vehicle.
    order = np.argsort(partitions, kind="stable")
    retained = retained[order]
    start, end, headings, partitions = start[order], end[order], headings[order], partitions[order]
    index = build_cross_track_index(
        start, end, partitions, cell_m=cell_m, max_cells=max_cells,
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
            "segment_identity_a": identities[original_a],
            "segment_identity_b": identities[original_b],
        }


def iter_grid_candidates(segments, crossing_batches, transformer):
    """Yield aligned candidate arrays, preserving source and segment identities."""
    size = len(segments["subtrack_index"])
    columns = {
        "vehicle_id": np.asarray(segments["vehicle_id"], dtype=object),
        "subtrack_index": segments["subtrack_index"],
        "point_index": segments["start_refined_sequence_in_subtrack"],
        "point_kind": np.where(np.asarray(segments["start_point_kind"]) == "kept", "p", "i"),
        "refine_point_id": segments["start_point_id"],
        "refine_point_id_valid": np.ones(size, dtype=np.bool_),
        "source_id": np.asarray(segments["start_source_id"], dtype=object),
        "source_row_key": np.asarray(segments["start_source_row_key"], dtype=np.int64),
        "source_row_key_valid": np.asarray(segments["start_point_kind"]) == "kept",
        "generated_reason": np.asarray(segments["start_generated_reason"], dtype=object),
        "timestamp": segments["timestamp"], "heading": segments["heading"], "speed": segments["speed"],
    }
    for batch in crossing_batches:
        a, b = batch["segment_index_a"], batch["segment_index_b"]
        yield build_cross_track_patch_candidates(columns, a, b, batch["x"], batch["y"], np.arange(len(a)), transformer)

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
        return concatenate_cross_batches([])

    thread_state = local()
    opened_connections = []
    connection_lock = Lock()
    budget_lock = Lock()
    result_count = result_bytes = done_count = segment_count = 0
    attempts = {}
    batches = []
    seen_pairs = np.empty((0, 4), dtype=np.int64)

    def worker(grid_index):
        nonlocal result_count, result_bytes
        conn = getattr(thread_state, "connection", None)
        if conn is None or conn.closed:
            conn = connect_worker()
            thread_state.connection = conn
            with connection_lock:
                opened_connections.append(conn)
        found = []
        reserved_count = reserved_bytes = 0
        try:
            with conn.transaction():
                segments = build_grid_segments(fetch_points_in_grid(conn, grid_index, grids, refine_id, spatial_context), max_segments=max_segments, max_bytes=max_segment_bytes)
                crossings = iter_grid_crossings(segments, cfg, max_grid_memberships=max_grid_memberships, max_overflow_pair_space=max_overflow_pair_space)
                for batch in iter_grid_candidates(segments, crossings, transformer):
                    # Array buffers plus object-string slots; no candidate object graph.
                    cost = sum(getattr(batch, name).nbytes for name in batch.__dataclass_fields__)
                    cost += 256 * len(batch)
                    with budget_lock:
                        if result_count + len(batch) > max_candidates or result_bytes + cost > max_result_bytes:
                            raise GridSegmentBudgetError("cross-track candidate result budget exceeded")
                        result_count += len(batch); result_bytes += cost
                    reserved_count += len(batch); reserved_bytes += cost
                    found.append(batch)
            return found, len(segments["subtrack_index"])
        except Exception:
            with budget_lock:
                result_count -= reserved_count; result_bytes -= reserved_bytes
            raise

    active = {}
    try:
        with ThreadPoolExecutor(max_workers=thread_count) as pool:
            while pending or active:
                while pending and len(active) < thread_count:
                    cell = pending.popleft()
                    grids[cell[0]][cell[1]].status = "running"
                    active[pool.submit(worker, cell)] = cell
                finished, _ = wait(active, timeout=3.0, return_when=FIRST_COMPLETED)
                now = time.monotonic()
                if now - last_log_at >= 3.0:
                    log_info(f"跨轨迹网格：{done_count}/{total} 完成，{len(active)} 运行中，耗时 {now-started:.1f}s")
                    last_log_at = now
                for future in finished:
                    cell = active.pop(future)
                    try:
                        found, count = future.result()
                    except Exception as exc:
                        if isinstance(exc, (GridSegmentBudgetError, CrossTrackCapacityError, MemoryError, ValueError, TypeError)) or attempts.get(cell, 0) >= 2:
                            raise
                        attempts[cell] = attempts.get(cell, 0) + 1
                        pending.appendleft(cell)
                        continue
                    for batch in found:
                        identities=np.column_stack((batch.from_subtrack_index,batch.to_subtrack_index,batch.from_point_index,batch.to_point_index))
                        all_identities = np.concatenate((seen_pairs, identities))
                        _, first = np.unique(all_identities, axis=0, return_index=True)
                        first.sort()
                        keep = first[first >= len(seen_pairs)] - len(seen_pairs)
                        seen_pairs = all_identities[first]
                        duplicates=len(batch)-len(keep)
                        if duplicates:
                            cost=sum(getattr(batch,name).nbytes for name in batch.__dataclass_fields__)+256*len(batch)
                            with budget_lock:
                                result_count-=duplicates; result_bytes-=cost*duplicates//max(1,len(batch))
                        if len(keep): batches.append(batch.take(np.asarray(keep,dtype=np.int64)))
                    grids[cell[0]][cell[1]].status = "done"
                    segment_count += count; done_count += 1
                if live_status is not None:
                    live_status.update(detail=f"跨轨迹网格：{done_count}/{total} 完成，{segment_count} 线段，{result_count} 候选", elapsed=time.monotonic()-started, stage="Detect: 跨轨迹网格")
    finally:
        for conn in opened_connections:
            conn.close()
    combined = concatenate_cross_batches(batches)
    if len(combined):
        identity = np.column_stack((combined.from_subtrack_index, combined.to_subtrack_index, combined.from_point_index, combined.to_point_index))
        _, unique = np.unique(identity, axis=0, return_index=True)
        combined = combined.take(unique)
        order = np.lexsort((combined.to_point_index, combined.from_point_index, combined.to_subtrack_index, combined.from_subtrack_index))
        combined = replace(combined.take(order), calculation_index=np.arange(len(combined), dtype=np.int64))
    if candidate_store is not None:
        candidate_store.extend([combined]); candidate_store.seal()
        return candidate_store
    return combined
