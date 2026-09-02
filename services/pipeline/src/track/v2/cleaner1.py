"""Backward-compatible Cleaner 1 exports.

The PRD-aligned implementation lives in ``track.v2.cleaner``. This module is a
thin adapter for older local imports.
"""

from __future__ import annotations

from .cleaner import (
    _exclude_stationary,
    _inject_orthogonal as _inject_orthogonal_new,
    _split_by_gaps,
    angle_delta_deg,
    clean,
    heading_vector,
    ray_intersection,
)
from .models import CleanerConfig, CleanerResult, ExcludedIndex


def _angle_delta_deg(start: float, end: float) -> float:
    return angle_delta_deg(start, end)


def _inject_orthogonal(points, cfg, *, spatial_context):
    injected_subtrack, injected_count, _injected_points = _inject_orthogonal_new(
        points,
        cfg,
        spatial_context=spatial_context,
    )
    return injected_subtrack, injected_count


def _ray_intersection(a_xy, a_heading_deg, b_xy, b_heading_deg):
    hit = ray_intersection(
        a_xy,
        heading_vector((a_heading_deg + 180.0) % 360.0),
        b_xy,
        heading_vector((b_heading_deg + 180.0) % 360.0),
    )
    if hit is None:
        return None
    x, y, ta, tb = hit
    if ta < 0.0 or tb < 0.0:
        return None
    return x, y


__all__ = [
    "CleanerConfig",
    "CleanerResult",
    "ExcludedIndex",
    "_angle_delta_deg",
    "_exclude_stationary",
    "_inject_orthogonal",
    "_ray_intersection",
    "_split_by_gaps",
    "clean",
]
