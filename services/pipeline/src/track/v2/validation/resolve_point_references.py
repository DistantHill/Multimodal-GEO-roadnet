"""Replay Refine groups using source columns and persisted partition identity."""
from __future__ import annotations
import numpy as np
from ..models import RefineResult, ReplayRefineResult, SourceBatch


def replay_refine(vehicle_id: str, tracks: dict[str, SourceBatch], *,
                  refine_result: RefineResult | None = None) -> ReplayRefineResult:
    source = tracks[vehicle_id]
    exclusions = {} if refine_result is None else {
        str(batch.source_row_key[i]): str(batch.reason[i])
        for batch in refine_result.excluded if batch.source_id == source.source_id
        for i in range(len(batch))}
    excluded_indices = np.asarray([i for i, key in enumerate(source.source_row_key)
                                   if str(key) in exclusions], dtype=np.int64)
    excluded_reasons = np.asarray([exclusions[str(source.source_row_key[i])]
                                  for i in excluded_indices], dtype=object)
    active = [] if refine_result is None else [track for track in refine_result.subtracks
                                               if track.vehicle_id == vehicle_id]
    return ReplayRefineResult(vehicle_id, source, excluded_indices, excluded_reasons, active)


def replay_refine_all(tracks: dict[str, SourceBatch], *, refine_result: RefineResult):
    return {vehicle_id: replay_refine(vehicle_id, tracks, refine_result=refine_result)
            for vehicle_id in tracks}
