"""Replay Refine groups using source columns and persisted partition identity."""
from __future__ import annotations
import numpy as np
from ..models import RefineResult, ReplayRefineResult, SourceBatch


def replay_refine(vehicle_id: str, tracks: dict[str, SourceBatch], *,
                  refine_result: RefineResult | None = None) -> ReplayRefineResult:
    source = tracks[vehicle_id]
    matching = [] if refine_result is None else [
        batch for batch in refine_result.excluded if batch.source_id == source.source_id
        and len(batch)]
    excluded_indices = np.empty(0, dtype=np.int64)
    excluded_reasons = np.empty(0, dtype=str)
    if matching:
        keys = np.concatenate([batch.source_row_key for batch in matching])
        reasons = np.concatenate([batch.reason for batch in matching])
        # Keep the last reason for duplicate keys, matching the former mapping.
        order = np.argsort(keys, kind="stable")
        keys, reasons = keys[order], reasons[order]
        last = np.r_[keys[1:] != keys[:-1], True]
        keys, reasons = keys[last], reasons[last]
        positions = np.searchsorted(keys, source.source_row_key)
        valid = positions < len(keys)
        valid[valid] &= keys[positions[valid]] == source.source_row_key[valid]
        excluded_indices = np.flatnonzero(valid).astype(np.int64, copy=False)
        excluded_reasons = reasons[positions[valid]]
    active = [] if refine_result is None else [track for track in refine_result.subtracks
                                               if track.vehicle_id == vehicle_id]
    return ReplayRefineResult(vehicle_id, source, excluded_indices, excluded_reasons, active)


def replay_refine_all(tracks: dict[str, SourceBatch], *, refine_result: RefineResult):
    return {vehicle_id: replay_refine(vehicle_id, tracks, refine_result=refine_result)
            for vehicle_id in tracks}
