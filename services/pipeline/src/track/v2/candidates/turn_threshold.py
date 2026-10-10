"""turn_threshold: select turn candidates by angle without computing Getis-Ord G*."""
from __future__ import annotations

import math
import numpy as np

from ..models import TurnBatch
from ..telemetry.heartbeat import operation_heartbeat
from .detect_turn_events import concatenate_turn_batches


def select_turn_threshold_events(events, cfg, **_kwargs) -> TurnBatch:
    """Keep abs(turn_deg) > min_turn_deg in original order and retain provenance."""
    threshold = cfg.min_turn_deg
    if not math.isfinite(threshold) or not 0 <= threshold <= 180:
        raise ValueError("turn_threshold min_turn_deg must be finite and within [0, 180]")
    batches = iter((events,)) if isinstance(events, TurnBatch) else events.iter_batches()
    pieces = []
    try:
        with operation_heartbeat("turn_threshold：按转向角筛选"):
            for batch in batches:
                keep = np.flatnonzero(np.abs(batch.turn_deg) > threshold)
                if len(keep):
                    pieces.append(batch.take(keep))
    finally:
        close = getattr(batches, "close", None)
        if close is not None:
            close()
    return concatenate_turn_batches(pieces)
