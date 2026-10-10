"""Angle threshold plugin with extraction reuse inside compatible parameter groups."""
from dataclasses import dataclass, fields
import math
import numpy as np
from ..plugins.core import CandidatePlugin
from ..plugins.execution import CandidateResult, FilteredTurnRelation


@dataclass(frozen=True)
class TurnThresholdParams:
    turn_window: int
    min_turn_deg: float
    max_turn_deg: float
    min_leg_m: float
    min_movement_speed_mps: float

    def __post_init__(self):
        if isinstance(self.turn_window, bool) or not isinstance(self.turn_window, int) or self.turn_window < 1:
            raise ValueError("turn_window must be a positive integer")
        for name in ("min_turn_deg", "max_turn_deg", "min_leg_m", "min_movement_speed_mps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.min_leg_m <= 0:
            raise ValueError("min_leg_m must be positive")
        if not self.min_turn_deg <= self.max_turn_deg <= 180:
            raise ValueError("min_turn_deg and max_turn_deg must satisfy 0 <= min <= max <= 180")


def run(context, params, requested_instances):
    groups = {}
    for name in requested_instances:
        instance = params[name]
        key = tuple(getattr(instance, item.name) for item in fields(instance) if item.name != "min_turn_deg")
        groups.setdefault(key, []).append(name)
    results = {}
    for names in groups.values():
        broadest = min(names, key=lambda name: params[name].min_turn_deg)
        parent = context.read_turn_events(params[broadest])
        # The reader uses inclusive extraction. Every output keeps the strict plugin boundary.
        for name in names:
            threshold = params[name].min_turn_deg
            events = parent if name == broadest else context.filter_turn_events(parent, lambda batch, threshold=threshold: np.abs(batch.turn_deg) >= threshold)
            candidates = FilteredTurnRelation(events, threshold)
            results[name] = CandidateResult("turn_threshold", name, "turn", candidates, events, params[name])
    return results


PLUGIN = CandidatePlugin("turn_threshold", TurnThresholdParams, "candidate_points", run)
