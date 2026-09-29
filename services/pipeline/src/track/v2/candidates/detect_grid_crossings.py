"""Detect and schedule exact cross-track candidates within XY grids."""
from __future__ import annotations

import math
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from queue import Empty, Queue
from threading import Lock, local
from typing import Any, Callable

import numpy as np

from ..db.fetch_refine_points_in_grid import fetch_points_in_grid
from ..models import CrossCandidateBatch
from .._geometry import _endpoint_bearings
from ..projection import CoordinateTransformer
from ..telemetry.console_logging import log_info
from .build_patches import (
    build_cross_track_index, iter_cross_track_filtered_patches,
)
from .detect_cross_track_pairs import (intersect_cross_track_patch, build_cross_track_patch_candidates, concatenate_cross_batches)
from .cross_track_timing import CrossTrackTiming
from .grid import create_grid
from .grid_segments import GridSegmentBudgetError, GridSegmentRelation, open_grid_segments
from .build_patches import _iter_cross_subtrack_pair_batches, _iter_pair_batches
from .temp_quota import is_temp_quota_exhausted


def iter_grid_crossings(
    segments: Mapping[str, np.ndarray | tuple[Any, ...]],
    cfg: Any,
    *,
    max_grid_memberships: int,
    max_overflow_pair_space: int,
    timing: CrossTrackTiming | None = None,
) -> Iterator[dict[str, np.ndarray | tuple[Any, ...]]]:
    """Yield bounded exact cross-vehicle hits from one grid's segment columns.

    Straightness follows the existing Detect heading rule. The current
    spatial bucket index creates bounded pair batches; oversized membership
    graphs use the exact overflow stream instead of rejecting total work. Each yield
    contains at most ``cfg.cross_track_pair_batch_size`` exact hits and retains
    original segment column indices plus stable identities for later grid merge.
    """
    timing = timing or CrossTrackTiming()
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
        "x_start", "y_start", "x_end", "y_end", "heading", "end_heading", "speed", "end_speed",
    )
    if len(identities) != size or any(len(segments[name]) != size for name in required):
        raise ValueError("segment columns have inconsistent lengths")
    if size < 2:
        return
    with timing.measure("prepare_s"):
        heading = np.asarray(segments["heading"], dtype=np.float64)
        end_heading = np.asarray(segments["end_heading"], dtype=np.float64)
        delta = (end_heading - heading + 180.0) % 360.0 - 180.0
        mean_speed = (np.asarray(segments["speed"], dtype=np.float64)
                      + np.asarray(segments["end_speed"], dtype=np.float64)) / 2.0
        speed_ok = mean_speed >= cfg.cross_track_min_avg_speed
        timing.values["speed_rejected_segments"] = int(np.count_nonzero(~speed_ok))
        straight = np.abs(delta) <= threshold
        timing.values["straight_segments"] = int(np.count_nonzero(straight))
        retained = np.flatnonzero(straight & speed_ok)
        timing.values["eligible_segments"] = len(retained)
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
        bearings = _endpoint_bearings(start, end)
        if not (np.isfinite(start).all() and np.isfinite(end).all()
                and np.isfinite(bearings).all()):
            raise ValueError("segment coordinates and headings must be finite")
        partitions = np.asarray(segments["subtrack_index"], dtype=np.int64)[retained]
        # Pair ordering follows the Run-scoped subtrack identity, independently of vehicle.
        order = np.argsort(partitions, kind="stable")
        retained = retained[order]
        start, end, bearings, partitions = start[order], end[order], bearings[order], partitions[order]
    with timing.measure("index_s"):
        index = build_cross_track_index(
            start, end, partitions, cell_m=cell_m, max_cells=max_cells,
            max_grid_memberships=max_grid_memberships,
            max_overflow_pair_space=max_overflow_pair_space, telemetry=timing.values,
        )
    for ids_a, ids_b in timing.iterate(iter_cross_track_filtered_patches(
            index, start, end, bearings, cfg, telemetry=timing.values), "candidate_s"):
        xs, ys, hit_indices = intersect_cross_track_patch(
            start, end, bearings, ids_a, ids_b, cfg, timing=timing,
        )
        if len(hit_indices) == 0:
            continue
        with timing.measure("output_s"):
            original_a = retained[ids_a[hit_indices]]
            original_b = retained[ids_b[hit_indices]]
            result = {
                "x": xs, "y": ys,
                "segment_index_a": original_a,
                "segment_index_b": original_b,
                "segment_identity_a": identities[original_a],
                "segment_identity_b": identities[original_b],
            }
        yield result


def iter_grid_candidates(segments, crossing_batches, transformer, *, timing: CrossTrackTiming | None = None):
    """Yield aligned candidate arrays, preserving source and segment identities."""
    timing = timing or CrossTrackTiming()
    with timing.measure("output_s"):
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
        with timing.measure("output_s"):
            result = build_cross_track_patch_candidates(columns, a, b, batch["x"], batch["y"], np.arange(len(a)), transformer)
        yield result


def iter_spilled_grid_candidates(relation: GridSegmentRelation, cfg, transformer, *, block_size: int = 4096, timing: CrossTrackTiming | None = None):
    """Compare every distinct block pair without retaining the whole grid."""
    if block_size < 1:
        raise ValueError("segment block size must be positive")
    timing = timing or CrossTrackTiming()
    pair_size = int(cfg.cross_track_pair_batch_size)
    threshold = float(cfg.cross_track_straightness_deg)
    total = relation.count
    for first in range(0, total, block_size):
        with timing.measure("gather_s"):
            left = relation.block(first, block_size)
            left_bearings = _endpoint_bearings(
                np.column_stack((left["x_start"], left["y_start"])),
                np.column_stack((left["x_end"], left["y_end"])),
            )
            speed_ok = (left["speed"] + left["end_speed"]) / 2.0 >= cfg.cross_track_min_avg_speed
            straight = np.abs((left["end_heading"] - left["heading"] + 180.0) % 360.0 - 180.0) <= threshold
            timing.values["speed_rejected_segments"] += int(np.count_nonzero(~speed_ok))
            timing.values["straight_segments"] += int(np.count_nonzero(straight))
            timing.values["eligible_segments"] += int(np.count_nonzero(straight & speed_ok))
        for second in range(first, total, block_size):
            with timing.measure("gather_s"):
                right = left if first == second else relation.block(second, block_size)
            with timing.measure("gather_s"):
                if first == second:
                    segments = left
                    bearings = left_bearings
                    pairs = _iter_cross_subtrack_pair_batches(
                        np.arange(len(left["subtrack_index"]), dtype=np.int64),
                        left["subtrack_index"], pair_size,
                    )
                else:
                    segments = {name: np.concatenate((left[name], right[name])) for name in left}
                    right_bearings = _endpoint_bearings(
                        np.column_stack((right["x_start"], right["y_start"])),
                        np.column_stack((right["x_end"], right["y_end"])),
                    )
                    bearings = np.concatenate((left_bearings, right_bearings))
                    base = len(left["subtrack_index"])
                    pairs = _iter_pair_batches(
                        np.arange(base, dtype=np.int64),
                        np.arange(base, len(segments["subtrack_index"]), dtype=np.int64),
                        pair_size,
                    )
                headings = np.asarray(segments["heading"], dtype=np.float64)
                end_heading = np.asarray(segments["end_heading"], dtype=np.float64)
                straight = np.abs((end_heading - headings + 180.0) % 360.0 - 180.0) <= threshold
                straight &= (segments["speed"] + segments["end_speed"]) / 2.0 >= cfg.cross_track_min_avg_speed
                starts = np.column_stack((segments["x_start"], segments["y_start"]))
                ends = np.column_stack((segments["x_end"], segments["y_end"]))
                if not (np.isfinite(starts[straight]).all() and np.isfinite(ends[straight]).all()
                        and np.isfinite(headings[straight]).all()):
                    raise ValueError("segment coordinates and headings must be finite")
            for a, b in timing.iterate(pairs, "candidate_s"):
                with timing.measure("candidate_s"):
                    keep = straight[a] & straight[b]
                    keep &= segments["subtrack_index"][a] != segments["subtrack_index"][b]
                    if not np.any(keep):
                        continue
                    a, b = a[keep], b[keep]
                xs, ys, hit = intersect_cross_track_patch(starts, ends, bearings, a, b, cfg, timing=timing)
                if not len(hit):
                    continue
                batch = {"x": xs, "y": ys, "segment_index_a": a[hit], "segment_index_b": b[hit]}
                yield from iter_grid_candidates(segments, (batch,), transformer, timing=timing)

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
    runtime: Any,
    data_root: Any,
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
                           sub_detail="当前grid(0)：", elapsed=0.0,
                           stage="Detect: 跨轨迹网格")
    if total == 0:
        if candidate_store is not None:
            candidate_store.seal()
            return candidate_store
        return concatenate_cross_batches([])

    thread_state = local()
    opened_connections = []
    connection_lock = Lock()
    result_count = done_count = segment_count = 0
    attempts: dict[tuple[int, int], int] = {}
    active: dict[Any, tuple[tuple[int, int], int, float]] = {}
    active_timings = {}
    active_segments: dict[tuple[int, int], int] = {}
    grid_order = {(col, row): ordinal for ordinal, (col, row) in enumerate(pending)}
    events: Queue[tuple[str, tuple[int, int], int, Any]] = Queue(maxsize=2)
    from threading import Event
    canceled = Event()
    staged = candidate_store is not None and hasattr(candidate_store, "append_staged")
    legacy_batches = []
    attempt_counts: dict[tuple[tuple[int, int], int], int] = {}

    def active_detail(now: float) -> str:
        lines = [f"当前grid({len(active)})："]
        for cell, _attempt, started_at in active.values():
            count = active_segments.get(cell)
            segments_text = f"{count:,}" if count is not None else "读取中"
            lines.append(
                f"  ({cell[0]}, {cell[1]}) 线段数：{segments_text}，"
                f"运行时间：{now - started_at:.1f}s\n    {active_timings[cell].summary()}"
            )
        return "\n".join(lines)

    def enqueue(kind, cell, attempt, payload):
        while not canceled.is_set():
            try:
                events.put((kind, cell, attempt, payload), timeout=0.1)
                return
            except Exception as error:
                from queue import Full
                if not isinstance(error, Full):
                    raise

    def worker(cell, attempt, timing):
        conn = getattr(thread_state, "connection", None)
        if conn is None or conn.closed:
            conn = connect_worker()
            thread_state.connection = conn
            with connection_lock:
                opened_connections.append(conn)
        local_index = 0
        with conn.transaction():
            with open_grid_segments(
                fetch_points_in_grid(conn, cell, grids, refine_id, spatial_context),
                max_segments=max_segments, max_bytes=max_segment_bytes,
                runtime=runtime, data_root=data_root, owner=f"grid-segments-{grid_order[cell]}",
                bounds=grids[cell[0]][cell[1]], timing=timing,
            ) as segments:
                if isinstance(segments, GridSegmentRelation):
                    timing.mode = "spill"
                    batches = iter_spilled_grid_candidates(segments, cfg, transformer, timing=timing)
                    count = segments.count
                else:
                    timing.mode = "memory"
                    crossings = iter_grid_crossings(
                        segments, cfg, max_grid_memberships=max_grid_memberships,
                        max_overflow_pair_space=max_overflow_pair_space, timing=timing,
                    )
                    batches = iter_grid_candidates(segments, crossings, transformer, timing=timing)
                    count = len(segments["subtrack_index"])
                enqueue("segments", cell, attempt, count)
                for batch in batches:
                    if canceled.is_set():
                        return count
                    with timing.measure("queue_s"):
                        enqueue("batch", cell, attempt, (local_index, batch))
                    local_index += len(batch)
        return count

    def process_event(event):
        nonlocal result_count
        kind, cell, attempt, payload = event
        if attempt != attempts.get(cell, 0):
            return
        if kind == "segments":
            active_segments[cell] = payload
        elif kind == "batch":
            local_index, batch = payload
            with active_timings[cell].measure("write_s"):
                if staged:
                    candidate_store.append_staged(grid_order[cell], local_index, batch)
                else:
                    legacy_batches.append((cell, attempt, batch))
            result_count += len(batch)
            attempt_counts[(cell, attempt)] = attempt_counts.get((cell, attempt), 0) + len(batch)

    try:
        with ThreadPoolExecutor(max_workers=thread_count) as pool:
            while pending or active or not events.empty():
                while pending and len(active) < thread_count:
                    cell = pending.popleft()
                    attempt = attempts.get(cell, 0)
                    grids[cell[0]][cell[1]].status = "running"
                    active_timings[cell] = CrossTrackTiming()
                    active[pool.submit(worker, cell, attempt, active_timings[cell])] = (cell, attempt, time.monotonic())
                try:
                    process_event(events.get(timeout=0.1))
                    while True:
                        process_event(events.get_nowait())
                except Empty:
                    pass
                now = time.monotonic()
                if now - last_log_at >= 3.0:
                    log_info(
                        f"跨轨迹网格：{done_count}/{total} 完成，{segment_count} 线段，"
                        f"{result_count} 候选，耗时 {now-started:.1f}s\n{active_detail(now)}"
                    )
                    last_log_at = now
                finished = [future for future in active if future.done()]
                # A completed worker has already put all its batches into the FIFO.
                # Drain before finish_grid or discard_grid, even after a failure.
                if finished:
                    while True:
                        try:
                            process_event(events.get_nowait())
                        except Empty:
                            break
                for future in finished:
                    cell, attempt, _started_at = active.pop(future)
                    active_segments.pop(cell, None)
                    try:
                        count = future.result()
                    except Exception as exc:
                        result_count -= attempt_counts.pop((cell, attempt), 0)
                        if staged:
                            candidate_store.discard_grid(grid_order[cell])
                        if (is_temp_quota_exhausted(exc)
                                or isinstance(exc, (GridSegmentBudgetError, MemoryError, ValueError, TypeError))
                                or attempts.get(cell, 0) >= 2):
                            raise
                        attempts[cell] = attempt + 1
                        pending.appendleft(cell)
                        continue
                    with active_timings[cell].measure("write_s"):
                        if staged:
                            candidate_store.finish_grid(grid_order[cell])
                    log_info(f"cross-track grid={cell} segments={count:,} "
                             f"elapsed_s={time.monotonic()-_started_at:.3f} "
                             f"{active_timings[cell].summary()}")
                    del active_timings[cell]
                    grids[cell[0]][cell[1]].status = "done"
                    segment_count += count
                    done_count += 1
                if live_status is not None:
                    now = time.monotonic()
                    live_status.update(
                        detail=f"跨轨迹网格：{done_count}/{total} 完成，{segment_count} 线段，{result_count} 候选",
                        sub_detail=active_detail(now), elapsed=now-started,
                        stage="Detect: 跨轨迹网格",
                    )
    finally:
        canceled.set()
        for conn in opened_connections:
            conn.close()

    finalize_started = time.monotonic()
    if staged:
        candidate_store.finalize_staged()
        log_info(f"cross-track finalize output_s={time.monotonic()-finalize_started:.3f}")
        return candidate_store
    combined = concatenate_cross_batches([
        batch for cell, attempt, batch in legacy_batches
        if attempt == attempts.get(cell, 0)
    ])
    if len(combined):
        identity = np.column_stack((combined.from_subtrack_index, combined.to_subtrack_index, combined.from_point_index, combined.to_point_index))
        _, unique = np.unique(identity, axis=0, return_index=True)
        combined = combined.take(unique)
        order = np.lexsort((combined.to_point_index, combined.from_point_index, combined.to_subtrack_index, combined.from_subtrack_index))
        combined = replace(combined.take(order), calculation_index=np.arange(len(combined), dtype=np.int64))
    if candidate_store is not None:
        candidate_store.extend([combined]); candidate_store.seal()
        log_info(f"cross-track finalize output_s={time.monotonic()-finalize_started:.3f}")
        return candidate_store
    log_info(f"cross-track finalize output_s={time.monotonic()-finalize_started:.3f}")
    return combined
