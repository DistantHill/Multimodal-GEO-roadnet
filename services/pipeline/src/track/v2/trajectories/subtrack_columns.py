"""Construct frozen Refine subtrack columns from ordered persisted columns."""
from __future__ import annotations

import numpy as np
from ..models import RefinementSubtrack


def refinement_subtrack_from_columns(*, vehicle_id: str, subtrack_index: int,
                                    source_id: str, xy: np.ndarray, lonlat: np.ndarray,
                                    timestamp: np.ndarray, heading: np.ndarray, speed: np.ndarray,
                                    kind: np.ndarray, source_row_key: np.ndarray,
                                    refine_point_id: np.ndarray, after_sequence: np.ndarray,
                                    generated_reason: np.ndarray) -> RefinementSubtrack:
    kind = np.asarray(kind)
    kept = (kind == 'p') | (kind == 'kept') | (kind == 0)
    kind = np.where(kept, 'p', 'i')
    size = len(kind)
    keys = np.asarray(source_row_key, dtype=np.int64)
    anchors = np.asarray(after_sequence, dtype=np.int64)
    return RefinementSubtrack(
        vehicle_id, subtrack_index, source_id, np.asarray(xy, dtype=np.float64),
        np.asarray(lonlat, dtype=np.float64), np.asarray(timestamp, dtype=np.float64),
        np.asarray(heading, dtype=np.float64), np.asarray(speed, dtype=np.float64), kind,
        keys, kept, np.asarray(refine_point_id, dtype=np.int64), np.ones(size, dtype=bool),
        np.asarray(generated_reason, dtype=object), anchors,
    )
