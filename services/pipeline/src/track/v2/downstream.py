from __future__ import annotations

from typing import Any

from .models import TrajectorySourceHandle
from .clip import extract_clips


def detect_intersections(active: TrajectorySourceHandle, *, config: Any) -> Any:
    raise NotImplementedError("intersection detection is outside Parser 0 + Cleaner 1 scope")


def fit_centerlines(clips: Any, *, config: Any) -> Any:
    raise NotImplementedError("centerline fitting is outside Parser 0 + Cleaner 1 scope")


def build_network(fits: Any, intersections: Any, *, config: Any) -> Any:
    raise NotImplementedError("network assembly is outside Parser 0 + Cleaner 1 scope")
