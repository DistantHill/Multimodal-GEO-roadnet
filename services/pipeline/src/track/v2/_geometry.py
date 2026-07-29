"""Shared geometry helpers for v2 pipeline stages.

Private module — all functions are underscore-prefixed. Imported by
cleaner1.py and intersect2.py to avoid duplication.

Provides:
  - Coordinate conversion (lon/lat <-> local meter x/y)
  - Distance, bearing, angle arithmetic
  - Segment-circle intersection (scalar + NumPy-batched)
  - Circular mean for angular statistics
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np


# ── Earth radius (approximate, city-scale) ────────────────────────────

_EARTH_R = 6_371_000.0


# ── Coordinate conversion ─────────────────────────────────────────────


def _to_xy(lon: float, lat: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    """Approximate local lon/lat to meter coordinates around a city-scale origin."""
    lat0 = math.radians(origin_lat)
    x = _EARTH_R * math.radians(lon - origin_lon) * math.cos(lat0)
    y = _EARTH_R * math.radians(lat - origin_lat)
    return x, y


def _to_lonlat(x: float, y: float, origin_lon: float, origin_lat: float) -> tuple[float, float]:
    """Convert local meter coordinates back to lon/lat."""
    lat0 = math.radians(origin_lat)
    lon = origin_lon + math.degrees(x / (_EARTH_R * math.cos(lat0)))
    lat = origin_lat + math.degrees(y / _EARTH_R)
    return lon, lat


# ── Distance & bearing ────────────────────────────────────────────────


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Euclidean distance in meters between two local-meter points."""
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Compass bearing from a to b in degrees: north=0, east=90."""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


# ── Angle arithmetic ──────────────────────────────────────────────────


def _angle_delta_deg(start: float, end: float) -> float:
    """Signed shortest angle from start to end, in [-180, 180)."""
    return (end - start + 180.0) % 360.0 - 180.0


def _circular_mean_deg(angles: Iterable[float]) -> float:
    """Circular mean of angles in degrees [0, 360).

    Raises ValueError if the iterable is empty.
    """
    values = list(angles)
    if not values:
        raise ValueError("at least one angle is required for circular mean")
    sx = sum(math.sin(math.radians(a)) for a in values)
    cy = sum(math.cos(math.radians(a)) for a in values)
    return (math.degrees(math.atan2(sx, cy)) + 360.0) % 360.0


# ── Segment bearing extraction ────────────────────────────────────────


def _segment_bearings(
    xy: list[tuple[float, float]], start: int, stop: int
) -> list[float]:
    """Bearings for segment indices [start, stop).

    Skips segments shorter than 1e-6 m to avoid numerical noise.
    """
    bearings: list[float] = []
    lo = max(0, start)
    hi = min(len(xy) - 1, stop)
    for i in range(lo, hi):
        if _distance_m(xy[i], xy[i + 1]) > 1e-6:
            bearings.append(_bearing_deg(xy[i], xy[i + 1]))
    return bearings


# ── Batch segment-segment intersection (Method 3) ──────────────────────


def _batch_segment_intersections(
    a1: np.ndarray,
    a2: np.ndarray,
    ha: np.ndarray,
    b1: np.ndarray,
    b2: np.ndarray,
    hb: np.ndarray,
    min_angle_deg: float,
    max_angle_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """NumPy batched segment-segment intersection + orthogonal angle filter.

    ZH: 向量化段到段求交。输入两组段 (A: N 个, B: M 个)，一次 broadcasting
        完成所有 N×M 组交点计算，同时过滤夹角不在 ``[min, max]`` 范围内的结果。

    Args:
        a1/a2: ``(N, 2)`` segment A start/end coords in meters.
        ha: ``(N,)`` heading of each segment A (degrees).
        b1/b2: ``(M, 2)`` segment B start/end coords in meters.
        hb: ``(M,)`` heading of each segment B (degrees).
        min_angle_deg, max_angle_deg: orthogonal angle range.

    Returns:
        ``(cx, cy, flat_idx)`` where cx/cy are ``(N, M)`` arrays (NaN where
        no valid crossing), and flat_idx are flat indices of valid hits into
        the (N, M) grid: ``si = fi // M, sj = fi % M``.
    """
    da = a2 - a1          # (N, 2)
    db = b2 - b1          # (M, 2)
    # Broadcast to (N, M)
    det = da[:, None, 0] * (-db[None, :, 1]) - (-db[None, :, 0]) * da[:, None, 1]
    parallel = np.abs(det) < 1e-12
    rhs_x = b1[None, :, 0] - a1[:, None, 0]
    rhs_y = b1[None, :, 1] - a1[:, None, 1]
    ta_num = rhs_x * (-db[None, :, 1]) - (-db[None, :, 0]) * rhs_y
    tb_num = da[:, None, 0] * rhs_y - rhs_x * da[:, None, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ta = np.where(~parallel, ta_num / det, np.nan)
        tb = np.where(~parallel, tb_num / det, np.nan)
    # Bounds + angle filter
    valid = (~parallel) & (ta >= 0.0) & (ta <= 1.0) & (tb >= 0.0) & (tb <= 1.0)
    angle = np.abs((hb[None, :] - ha[:, None] + 180.0) % 360.0 - 180.0)
    valid &= (min_angle_deg <= angle) & (angle <= max_angle_deg)
    cx = np.where(valid, a1[:, None, 0] + ta * da[:, None, 0], np.nan)
    cy = np.where(valid, a1[:, None, 1] + ta * da[:, None, 1], np.nan)
    flat_idx = np.flatnonzero(valid)
    return cx, cy, flat_idx


# ── Segment-circle intersection (scalar) ──────────────────────────────


def _segment_circle_crossing(
    a: tuple[float, float],
    b: tuple[float, float],
    center: tuple[float, float],
    radius_m: float,
) -> tuple[float, float] | None:
    """Return the first intersection point between segment ab and a circle.

    Returns None if the segment does not intersect the circle.
    For two-hit segments the first intersection along a→b is returned.
    """
    cx, cy = center
    ax = a[0] - cx
    ay = a[1] - cy
    bx = b[0] - cx
    by = b[1] - cy
    dx = bx - ax
    dy = by - ay

    qa = dx * dx + dy * dy
    qb = 2.0 * (ax * dx + ay * dy)
    qc = ax * ax + ay * ay - radius_m * radius_m
    disc = qb * qb - 4.0 * qa * qc
    if qa == 0.0 or disc < 0.0:
        return None

    sqrt_disc = math.sqrt(disc)
    roots = sorted(
        ((-qb - sqrt_disc) / (2.0 * qa), (-qb + sqrt_disc) / (2.0 * qa))
    )
    for t in roots:
        if 0.0 <= t <= 1.0:
            return cx + ax + t * dx, cy + ay + t * dy
    return None


# ── Segment-circle intersection (NumPy-batched) ───────────────────────


def _segment_circle_crossings_batch(
    a_xy: np.ndarray,
    b_xy: np.ndarray,
    center: tuple[float, float],
    radius_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized first intersections between many segments and one circle.

    Args:
        a_xy: ``(n, 2)`` segment start coordinates in meters.
        b_xy: ``(n, 2)`` segment end coordinates in meters.
        center: Circle center in the same meter coordinate system.
        radius_m: Circle radius in meters.

    Returns:
        ``(hit_mask, crossing_xy)`` where ``hit_mask`` is a boolean ``(n,)``
        array and ``crossing_xy`` is an ``(n, 2)`` array. Misses are filled
        with ``nan``; the first intersection along a→b is returned per segment.
    """
    a = np.asarray(a_xy, dtype=float)
    b = np.asarray(b_xy, dtype=float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 2:
        raise ValueError("a_xy and b_xy must have matching shape (n, 2)")

    n = a.shape[0]
    if n == 0:
        return np.zeros(0, dtype=bool), np.empty((0, 2), dtype=float)

    center_xy = np.asarray(center, dtype=float)
    rel_a = a - center_xy
    rel_b = b - center_xy
    delta = rel_b - rel_a

    qa = np.einsum("ij,ij->i", delta, delta)
    qb = 2.0 * np.einsum("ij,ij->i", rel_a, delta)
    qc = np.einsum("ij,ij->i", rel_a, rel_a) - radius_m * radius_m
    disc = qb * qb - 4.0 * qa * qc

    valid = (qa > 0.0) & (disc >= 0.0)
    sqrt_disc = np.zeros(n, dtype=float)
    sqrt_disc[valid] = np.sqrt(disc[valid])

    denom = np.ones(n, dtype=float)
    denom[valid] = 2.0 * qa[valid]
    t1 = np.full(n, np.nan, dtype=float)
    t2 = np.full(n, np.nan, dtype=float)
    t1[valid] = (-qb[valid] - sqrt_disc[valid]) / denom[valid]
    t2[valid] = (-qb[valid] + sqrt_disc[valid]) / denom[valid]

    t1_inside = (0.0 <= t1) & (t1 <= 1.0)
    t2_inside = (0.0 <= t2) & (t2 <= 1.0)
    hit_mask = valid & (t1_inside | t2_inside)
    t = np.where(t1_inside, t1, np.where(t2_inside, t2, np.nan))

    crossing_xy = np.full((n, 2), np.nan, dtype=float)
    crossing_xy[hit_mask] = (
        center_xy + rel_a[hit_mask] + t[hit_mask, None] * delta[hit_mask]
    )
    return hit_mask, crossing_xy


# ── Branch angle relative to center ───────────────────────────────────


def _branch_angle_from_center(
    center: tuple[float, float], boundary_point: tuple[float, float]
) -> float:
    """Bearing from center to boundary point, representing a branch direction."""
    return _bearing_deg(center, boundary_point)
