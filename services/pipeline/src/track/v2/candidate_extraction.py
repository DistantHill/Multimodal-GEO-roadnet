"""Run scheduled candidate patches and merge results before global clustering."""
from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, TYPE_CHECKING

import numpy as np

from ._perf import PerfTracker
from .candidate_patches import TrackPatch, build_cross_track_index, iter_cross_track_patches
from .cross_track import (
    _prepare_segments, detect_cross_track_patch, intersect_cross_track_patch,
    build_cross_track_patch_candidates,
)
from .heartbeat import heartbeat_progress, observed_operation, operation_heartbeat
from .logger import log_info
from .models import CrossTrackCandidate, TrajectoryPoint, TurnEvent
from .projection import CoordinateTransformer
from .turn_events import detect_turn_patch
from .turn_event_relation import TurnEventRelation
from .cross_member_store import CandidateStore

if TYPE_CHECKING:
    from .detect import IntersectConfig


def extract_turn_candidates(
    patches: Iterable[TrackPatch],
    cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
    event_store: TurnEventRelation | None = None,
) -> Sequence[TurnEvent]:
    """Consume all patches, preserving event order and assigning global IDs.

    Patches are still consumed with turn detection disabled so the DB reader
    can populate the workspace used by cross-track detection and topology.
    """
    events: list[TurnEvent] | TurnEventRelation = (
        [] if event_store is None else event_store
    )
    track_count = 0
    started = last_progress = time.perf_counter()
    log_info("── 轨迹处理 / Tracks and turn candidates ...")
    with operation_heartbeat("轨迹读取与转向候选提取"):
        for patch in patches:
            # The long-lived reader scope includes work performed across yield.
            # Keep its cumulative elapsed time and row count through every patch.
            if cfg.use_turn_events:
                for event in detect_turn_patch(patch, cfg, perf=perf):
                    numbered = replace(event, event_calculate_index=len(events))
                    if event_store is None:
                        events.append(numbered)
                    else:
                        event_store.append(numbered)
            track_count += len(patch)
            now = time.perf_counter()
            if live_status is not None and now - last_progress >= 3.0:
                live_status.update(
                    detail=f"轨迹处理：{track_count:,} 轨迹，{len(events):,} 转向事件",
                    elapsed=now - started,
                    stage="Detect: 转向候选 / Turn candidates",
                )
                last_progress = now
            # Do not retain the previous full patch while the producer builds
            # the next one. Events retain provenance, not trajectory objects.
            del patch
    if event_store is not None:
        event_store.seal()
    log_info(f"── 轨迹处理: {track_count:,} 轨迹/tracks → "
             f"{len(events):,} 转向事件/turn events "
             f"({time.perf_counter() - started:.1f}s)")
    return events


@observed_operation("跨轨迹候选提取")
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
    array_started = time.perf_counter() if telemetry is not None else 0.0
    with operation_heartbeat("跨轨迹空间网格构建"):
        index = build_cross_track_index(
            segments,
            cell_m=cfg.cross_track_grid_cell_m,
            max_cells=cfg.cross_track_max_grid_cells_per_segment,
            max_grid_memberships=max_grid_memberships,
            max_overflow_pair_space=max_overflow_pair_space,
            telemetry=telemetry,
        )
    starts = np.array([(s.x1, s.y1) for s in segments], dtype=float)
    ends = np.array([(s.x2, s.y2) for s in segments], dtype=float)
    headings = np.array([s.heading for s in segments], dtype=float)
    if telemetry is not None:
        telemetry["array_build_seconds"] = telemetry.get("array_build_seconds", 0.0) + (
            time.perf_counter() - array_started
        )
        telemetry["vehicle_pairs"] = telemetry.get("vehicle_pairs", 0) + total_pairs

    ordered_candidates: list[tuple[tuple[int, ...], CrossTrackCandidate]] = []
    started = time.perf_counter()
    last_progress = started
    completed_pairs = 0
    for ids_a, ids_b in iter_cross_track_patches(index, cfg.cross_track_pair_batch_size, perf=perf):
        ordered_candidates.extend(detect_cross_track_patch(
            segments, starts, ends, headings, ids_a, ids_b, cfg, transformer,
        ))
        completed_pairs += len(ids_a)
        heartbeat_progress(completed_pairs, unit="线段对")
        now = time.perf_counter()
        if live_status is not None and now - last_progress >= 3.0:
            detail = f"跨轨迹计算：{completed_pairs:,} 线段对，{len(ordered_candidates):,} 候选"
            if sub_detail:
                live_status.update(sub_detail=detail, elapsed=now - started)
            else:
                live_status.update(detail=detail, elapsed=now - started)
            last_progress = now

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


@observed_operation("列式跨轨迹候选提取")
def extract_columnar_cross_track_candidates(
    reader: Any,
    source_table: str,
    transformer: CoordinateTransformer,
    cfg: Any,
    *,
    live_status: Any = None,
    candidate_store: CandidateStore | None = None,
) -> Sequence[CrossTrackCandidate]:
    """Run the existing exact NumPy kernel on bounded DuckDB pair batches."""
    result: list[CrossTrackCandidate] | CandidateStore = (
        [] if candidate_store is None else candidate_store
    )
    pair_count = 0
    started = last_progress = time.perf_counter()
    batches = reader.iter_cross_track_pair_batches(
        source_table, cfg, batch_size=cfg.cross_track_pair_batch_size,
    )
    iterator = iter(batches)
    try:
        for batch in iterator:
            size = len(batch)
            segments = {
                index: segment
                for pair_index, pair in enumerate(batch)
                for index, segment in ((pair_index * 2, pair[0]),
                                       (pair_index * 2 + 1, pair[1]))
            }
            starts = np.asarray(
                [(segments[index].x1, segments[index].y1) for index in range(size * 2)],
                dtype=float,
            )
            ends = np.asarray(
                [(segments[index].x2, segments[index].y2) for index in range(size * 2)],
                dtype=float,
            )
            headings = np.asarray(
                [segments[index].heading for index in range(size * 2)], dtype=float,
            )
            pair_a = np.arange(0, size * 2, 2, dtype=np.int64)
            pair_b = pair_a + 1
            xs, ys, hit_indices = intersect_cross_track_patch(
                starts, ends, headings, pair_a, pair_b, cfg,
            )
            for _key, candidate in build_cross_track_patch_candidates(
                segments, pair_a, pair_b, xs, ys, hit_indices, transformer,
            ):
                numbered = replace(candidate, cross_track_calculate_index=len(result))
                result.append(numbered)
            pair_count += size
            heartbeat_progress(pair_count, unit="线段对")
            now = time.perf_counter()
            if live_status is not None and now - last_progress >= 3.0:
                live_status.update(
                    detail=f"跨轨迹计算：{pair_count:,} 线段对，{len(result):,} 候选",
                    elapsed=now - started,
                )
                last_progress = now
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    if candidate_store is not None:
        candidate_store.seal()
    log_info(f"── 列式跨轨迹: {pair_count:,} 线段对 → {len(result):,} 候选 "
             f"({time.perf_counter() - started:.1f}s)")
    return result


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


@observed_operation("磁盘空间索引跨轨迹候选提取")
def extract_disk_cross_track_candidates(
    workspace: Any,
    transformer: CoordinateTransformer,
    cfg: Any,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
    candidate_store: CandidateStore | None = None,
) -> Sequence[CrossTrackCandidate]:
    """Run the same pair kernel on disk-indexed bbox patches, then sort globally.

    DB mode uses an R-tree rather than replicated grid memberships. Grid cell
    size/overflow limits apply to the in-memory handle path only; pair batch size
    still bounds the exact kernel (with an 8192-pair metadata ceiling here).
    """
    pairs = candidates = 0
    started = last_progress = time.perf_counter()

    def progress_detail() -> str:
        return (f"源线段已扫描至少 {workspace.scanned_source_segments:,}/{workspace.segment_count:,}；"
                f"候选命中 {candidates:,}")

    log_info("── 跨轨迹：磁盘 R-tree 线段索引 / Disk segment index ...")
    heartbeat_progress(0, unit="线段对", detail=progress_detail())
    for patch in workspace.iter_cross_patches(cfg.cross_track_pair_batch_size):
        xs, ys, hit_indices = intersect_cross_track_patch(
            patch.starts, patch.ends, patch.headings, patch.pair_a, patch.pair_b, cfg,
        )
        segments = workspace.load_hit_segments(patch, hit_indices)
        hits = build_cross_track_patch_candidates(
            segments, patch.pair_a, patch.pair_b, xs, ys, hit_indices, transformer,
        )
        workspace.save_candidates(hits)
        pairs += len(patch.pair_a)
        candidates += len(hits)
        heartbeat_progress(pairs, unit="线段对", detail=progress_detail())
        now = time.perf_counter()
        if live_status is not None and now - last_progress >= 3.0:
            live_status.update(
                detail=f"跨轨迹计算：{pairs:,} 线段对，{candidates:,} 候选",
                elapsed=now - started,
            )
            last_progress = now
    heartbeat_progress(pairs, unit="线段对", detail=progress_detail())
    with operation_heartbeat("跨轨迹候选全局排序与编号"):
        result = [] if candidate_store is None else candidate_store
        iterator = iter(workspace.iter_candidates())
        try:
            for index, candidate in enumerate(iterator):
                result.append(replace(candidate, cross_track_calculate_index=index))
                heartbeat_progress(index + 1, unit="候选")
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        if candidate_store is not None:
            candidate_store.seal()
    log_info(f"── 磁盘跨轨迹: {pairs:,} 线段对 → {len(result):,} 候选 "
             f"({time.perf_counter() - started:.1f}s)")
    return result
