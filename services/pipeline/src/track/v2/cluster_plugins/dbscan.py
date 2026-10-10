"""DBSCAN description; algorithm import is deferred until execution."""
from dataclasses import dataclass
import math
from ..plugins.core import ClusterPlugin


@dataclass(frozen=True)
class DBSCANParams:
    eps_m: float
    min_samples: int

    def __post_init__(self):
        if isinstance(self.eps_m, bool) or not isinstance(self.eps_m, (int, float)) or not math.isfinite(self.eps_m) or self.eps_m <= 0:
            raise ValueError("eps_m must be finite and positive")
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int) or self.min_samples < 1:
            raise ValueError("min_samples must be a positive integer")


def run(x, y, *, params, **kwargs):
    from ..intersections.detect_intersections import _run_dbscan
    return _run_dbscan(x, y, eps_m=params.eps_m, min_samples=params.min_samples, **kwargs)


PLUGIN = ClusterPlugin("dbscan", DBSCANParams, "candidate_points", run)
