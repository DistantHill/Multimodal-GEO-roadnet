"""Existing orthogonal cross-track extraction adapted to parameter instances."""
from dataclasses import dataclass
import math
from ..plugins.core import CandidatePlugin
from ..plugins.execution import CandidateResult


@dataclass(frozen=True)
class CrossParams:
    cross_track_min_avg_speed: float
    cross_track_straightness_deg: float
    cross_track_min_angle_deg: float
    cross_track_max_angle_deg: float
    cross_track_grid_cell_m: float
    cross_track_pair_batch_size: int
    cross_track_max_grid_cells_per_segment: int
    cross_track_task_grid_m: float
    cross_track_task_threads: int

    def __post_init__(self):
        for name in ("cross_track_pair_batch_size", "cross_track_max_grid_cells_per_segment", "cross_track_task_threads"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("cross_track_min_avg_speed", "cross_track_straightness_deg", "cross_track_min_angle_deg", "cross_track_max_angle_deg", "cross_track_grid_cell_m", "cross_track_task_grid_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in ("cross_track_grid_cell_m", "cross_track_task_grid_m"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cross_track_task_threads > 8:
            raise ValueError("cross_track_task_threads must be at most 8")
        if not 0 <= self.cross_track_min_angle_deg <= self.cross_track_max_angle_deg <= 180:
            raise ValueError("cross-track angle range is invalid")
        if self.cross_track_straightness_deg > 180:
            raise ValueError("cross-track straightness must be at most 180")


def run(context, params, requested_instances):
    results = {}
    for name in requested_instances:
        events = context.run_cross(params[name])
        results[name] = CandidateResult("cross", name, "orthogonal_cross_track", events, events, params[name])
    return results


PLUGIN = CandidatePlugin("cross", CrossParams, "candidate_points", run)
