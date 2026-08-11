from __future__ import annotations

from typing import Any

from .models import TrajectorySourceHandle
from .clip import extract_clips
from .fitting import fit_centerlines
from .network import build_network


def detect_intersections(active: TrajectorySourceHandle, *, config: Any) -> Any:
    raise NotImplementedError("intersection detection is outside Parser 0 + Cleaner 1 scope")
