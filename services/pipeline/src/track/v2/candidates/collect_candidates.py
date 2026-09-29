"""Collect directly requested columnar cross-track candidates."""
from __future__ import annotations

import time
from dataclasses import replace

from ..telemetry.console_logging import log_info
from ..telemetry.heartbeat import heartbeat_progress, observed_operation
from .detect_cross_track_pairs import detect_cross_track_patch, concatenate_cross_batches


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
