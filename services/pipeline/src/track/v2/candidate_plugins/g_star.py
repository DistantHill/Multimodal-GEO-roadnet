"""Existing G* turn candidate selection, independently evaluated per instance."""
from dataclasses import dataclass
import math
from .turn_threehold import TurnThreeholdParams
from ..plugins.core import CandidatePlugin
from ..plugins.execution import CandidateResult


@dataclass(frozen=True)
class GStarParams(TurnThreeholdParams):
    g_star_radius_m: float
    g_star_min_z: float

    def __post_init__(self):
        super().__post_init__()
        if not math.isfinite(self.g_star_radius_m) or self.g_star_radius_m <= 0:
            raise ValueError("g_star_radius_m must be finite and positive")
        if not math.isfinite(self.g_star_min_z):
            raise ValueError("g_star_min_z must be finite")


def run(context, params, requested_instances):
    results = {}
    for name in requested_instances:
        events = context.read_turn_events(params[name])
        candidates = context.select_gstar(events, params[name])
        results[name] = CandidateResult("g_star", name, "turn", candidates, events, params[name])
    return results


PLUGIN = CandidatePlugin("g_star", GStarParams, "candidate_points", run)
