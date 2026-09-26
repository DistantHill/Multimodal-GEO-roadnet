"""Schedule array candidate kernels and collect business batches."""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import replace
import numpy as np
from ..telemetry.console_logging import log_info
from ..telemetry.heartbeat import heartbeat_progress, observed_operation, operation_heartbeat
from .build_patches import build_cross_track_index, iter_cross_track_patches
from .detect_cross_track_pairs import _prepare_segments, detect_cross_track_patch, concatenate_cross_batches
from .detect_turn_events import detect_turn_patch, concatenate_turn_batches


def extract_turn_candidates(patches, cfg, *, event_store=None, live_status=None, perf=None):
    batches = []
    count = track_count = 0
    started = last_progress = time.perf_counter()
    log_info("── 轨迹处理 / Tracks and turn candidates ...")
    with operation_heartbeat("轨迹读取与转向候选提取"):
        for patch in patches:
            if cfg.use_turn_events:
                batch = detect_turn_patch(patch, cfg, perf=perf)
                batch = replace(batch, calculation_index=batch.calculation_index + count)
                count += len(batch)
                if event_store is None:
                    batches.append(batch)
                else:
                    event_store.append_batch(batch)
            track_count += len(patch)
            now = time.perf_counter()
            if live_status is not None and now - last_progress >= 3.0:
                live_status.update(detail=f"轨迹处理：{track_count:,} 轨迹，{count:,} 转向事件",
                                   elapsed=now-started, stage="Detect: 转向候选 / Turn candidates")
                last_progress = now
            del patch
    if event_store is not None:
        event_store.seal()
    log_info(f"── 轨迹处理: {track_count:,} 轨迹/tracks → {count:,} 转向事件/turn events ({time.perf_counter()-started:.1f}s)")
    return event_store if event_store is not None else concatenate_turn_batches(batches)


@observed_operation("跨轨迹候选提取")
def extract_cross_track_candidates(tracks, transformer, cfg, live_status=None, *,
                                   max_grid_memberships=None, max_overflow_pair_space=None,
                                   telemetry=None, perf=None, sub_detail=False):
    extraction_started = time.perf_counter()
    segment_started = time.perf_counter()
    with perf.stage("segment_preparation") if perf is not None else nullcontext():
        columns, partitions = _prepare_segments(tracks, transformer, cfg)
    if telemetry is not None:
        telemetry["segment_build_seconds"] = telemetry.get("segment_build_seconds",0.0) + time.perf_counter()-segment_started
        telemetry["segments"] = telemetry.get("segments",0) + len(columns["starts"])
    array_started = time.perf_counter()
    with operation_heartbeat("跨轨迹空间网格构建"):
        index = build_cross_track_index(columns["starts"], columns["ends"], columns["subtrack_index"],
                                        cell_m=cfg.cross_track_grid_cell_m,
                                        max_cells=cfg.cross_track_max_grid_cells_per_segment,
                                        max_grid_memberships=max_grid_memberships,
                                        max_overflow_pair_space=max_overflow_pair_space, telemetry=telemetry)
    total_pairs = partitions*(partitions-1)//2
    if telemetry is not None:
        telemetry["array_build_seconds"] = telemetry.get("array_build_seconds",0.0) + time.perf_counter()-array_started
        telemetry["vehicle_pairs"] = telemetry.get("vehicle_pairs",0) + total_pairs
    batches = []
    completed_pairs = candidate_count = 0
    started = last_progress = time.perf_counter()
    iterator = iter(iter_cross_track_patches(index,cfg.cross_track_pair_batch_size,perf=perf))
    try:
        for ids_a,ids_b in iterator:
            batch = detect_cross_track_patch(columns,columns["starts"],columns["ends"],columns["heading"],ids_a,ids_b,cfg,transformer)
            batches.append(batch)
            candidate_count += len(batch)
            completed_pairs += len(ids_a)
            heartbeat_progress(completed_pairs,unit="线段对")
            now = time.perf_counter()
            if live_status is not None and now-last_progress >= 3.0:
                detail = f"跨轨迹计算：{completed_pairs:,} 线段对，{candidate_count:,} 候选"
                live_status.update(**({"sub_detail":detail} if sub_detail else {"detail":detail}),elapsed=now-started)
                last_progress = now
    finally:
        close = getattr(iterator,"close",None)
        if close is not None:
            close()
    result = concatenate_cross_batches(batches)
    if len(result):
        order = np.lexsort((result.to_point_index,result.from_point_index,result.to_subtrack_index,result.from_subtrack_index))
        result = replace(result.take(order),calculation_index=np.arange(len(result),dtype=np.int64))
    _emit_progress(live_status,total_pairs,total_pairs,len(result),time.perf_counter()-started,sub_detail=sub_detail,final=True)
    if telemetry is not None:
        telemetry["cross_track_seconds"] = telemetry.get("cross_track_seconds",0.0) + time.perf_counter()-extraction_started
        telemetry["output_candidates"] = telemetry.get("output_candidates",0) + len(result)
    return result,total_pairs,total_pairs


@observed_operation("列式轨迹交点候选提取")
def extract_columnar_cross_track_candidates(reader, source_table, transformer, cfg, *,
                                         candidate_store=None, live_status=None):
    batches = []
    count = pair_count = 0
    started = last_progress = time.perf_counter()
    iterator = iter(reader.iter_cross_track_pair_batches(source_table,cfg,batch_size=cfg.cross_track_pair_batch_size))
    try:
        for columns,ids_a,ids_b in iterator:
            batch = detect_cross_track_patch(columns,columns["starts"],columns["ends"],columns["heading"],ids_a,ids_b,cfg,transformer)
            batch = replace(batch,calculation_index=batch.calculation_index+count)
            count += len(batch)
            if candidate_store is None:
                batches.append(batch)
            else:
                candidate_store.append_batch(batch)
            pair_count += len(ids_a)
            heartbeat_progress(pair_count,unit="线段对")
            now = time.perf_counter()
            if live_status is not None and now-last_progress >= 3.0:
                live_status.update(detail=f"跨轨迹计算：{pair_count:,} 线段对，{count:,} 候选",elapsed=now-started)
                last_progress = now
    finally:
        close = getattr(iterator,"close",None)
        if close is not None:
            close()
    if candidate_store is not None:
        candidate_store.seal()
    log_info(f"── 列式跨轨迹: {pair_count:,} 线段对 → {count:,} 候选 ({time.perf_counter()-started:.1f}s)")
    return candidate_store if candidate_store is not None else concatenate_cross_batches(batches)


def _emit_progress(live_status,pair_count,total_pairs,candidate_count,elapsed,*,sub_detail=False,final=False):
    detail = f"跨轨迹求交/Cross-track:\t{pair_count:,}/{total_pairs:,}\t子轨迹对/pairs,\t{candidate_count:,}\t候选/candidates"
    line = detail if final else f"计算中：{detail}"
    if live_status is not None:
        if sub_detail:
            live_status.update(sub_detail=line,elapsed=elapsed)
        else:
            live_status.update(progress=pair_count/total_pairs if total_pairs else 0.0,detail=line,elapsed=elapsed)
    else:
        print(f"    ... {line} ({elapsed:.1f}s)")
    return detail
