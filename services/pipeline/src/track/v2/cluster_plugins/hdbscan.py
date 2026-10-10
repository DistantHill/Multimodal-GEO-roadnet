"""Existing HDBSCAN description preserving its current epsilon semantics."""
from ..plugins.core import ClusterPlugin
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class HDBSCANParams:
    eps_m: float
    min_samples: int

    def __post_init__(self):
        if isinstance(self.eps_m, bool) or not isinstance(self.eps_m, (int, float)) or not math.isfinite(self.eps_m) or self.eps_m < 0:
            raise ValueError("eps_m must be finite and non-negative")
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int) or self.min_samples < 1:
            raise ValueError("min_samples must be a positive integer")


def run(x, y, *, params, **kwargs):
    from ..candidates.hdbscan import run_hdbscan
    return run_hdbscan(x, y, eps_m=params.eps_m, min_samples=params.min_samples, **kwargs)


PLUGIN = ClusterPlugin("hdbscan", HDBSCANParams, "candidate_points", run)
