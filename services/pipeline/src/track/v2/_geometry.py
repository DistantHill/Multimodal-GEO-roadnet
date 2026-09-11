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
import time
from collections.abc import Iterable, Iterator

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
    points: list[tuple[float, float]], start: int, stop: int
) -> list[float]:
    """Bearings for segment indices [start, stop).

    Skips segments shorter than 1e-6 m to avoid numerical noise.
    """
    bearings: list[float] = []
    lo = max(0, start)
    hi = min(len(points) - 1, stop)
    for i in range(lo, hi):
        if _distance_m(points[i], points[i + 1]) > 1e-6:
            bearings.append(_bearing_deg(points[i], points[i + 1]))
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


def _iter_bounded_segment_intersections(
    a1: np.ndarray,
    a2: np.ndarray,
    ha: np.ndarray,
    b1: np.ndarray,
    b2: np.ndarray,
    hb: np.ndarray,
    min_angle_deg: float,
    max_angle_deg: float,
    *,
    max_matrix_elements: int = 262_144,
    dense_direct_threshold: int = 1_024,
    telemetry: dict[str, int | float] | None = None,
) -> Iterator[tuple[int, np.ndarray, np.ndarray, np.ndarray]]:
    """Yield bounded exact-kernel hits in baseline row-major order."""
    if max_matrix_elements < 1:
        raise ValueError("max_matrix_elements must be at least 1")
    if telemetry is not None:
        telemetry["matrix_element_cap"] = max_matrix_elements
    if len(a1) == 0 or len(b1) == 0:
        return

    pair_elements = len(a1) * len(b1)
    if pair_elements <= min(dense_direct_threshold, max_matrix_elements):
        exact_started = time.perf_counter() if telemetry is not None else 0.0
        cx, cy, flat_idx = _batch_segment_intersections(
            a1, a2, ha, b1, b2, hb, min_angle_deg, max_angle_deg,
        )
        if telemetry is not None:
            telemetry["dense_direct_pairs"] = telemetry.get("dense_direct_pairs", 0) + pair_elements
            telemetry["dense_direct_blocks"] = telemetry.get("dense_direct_blocks", 0) + 1
            telemetry["max_dense_direct_pairs"] = max(
                telemetry.get("max_dense_direct_pairs", 0), pair_elements,
            )
            telemetry["exact_seconds"] = telemetry.get("exact_seconds", 0.0) + (
                time.perf_counter() - exact_started
            )
            telemetry["exact_pairs"] = telemetry.get("exact_pairs", 0) + pair_elements
        if len(flat_idx):
            yield 0, cx.ravel()[flat_idx], cy.ravel()[flat_idx], flat_idx
        return

    columns_per_block = min(len(b1), max_matrix_elements)
    rows_per_block = max(1, max_matrix_elements // columns_per_block)
    for a_start in range(0, len(a1), rows_per_block):
        a_stop = min(len(a1), a_start + rows_per_block)
        broad_started = time.perf_counter() if telemetry is not None else 0.0
        a_min_x = np.minimum(a1[a_start:a_stop, 0], a2[a_start:a_stop, 0])
        a_max_x = np.maximum(a1[a_start:a_stop, 0], a2[a_start:a_stop, 0])
        a_min_y = np.minimum(a1[a_start:a_stop, 1], a2[a_start:a_stop, 1])
        a_max_y = np.maximum(a1[a_start:a_stop, 1], a2[a_start:a_stop, 1])
        for b_start in range(0, len(b1), columns_per_block):
            b_stop = min(len(b1), b_start + columns_per_block)
            b_min_x = np.minimum(b1[b_start:b_stop, 0], b2[b_start:b_stop, 0])
            b_max_x = np.maximum(b1[b_start:b_stop, 0], b2[b_start:b_stop, 0])
            b_min_y = np.minimum(b1[b_start:b_stop, 1], b2[b_start:b_stop, 1])
            b_max_y = np.maximum(b1[b_start:b_stop, 1], b2[b_start:b_stop, 1])
            broad = (
                (a_min_x[:, None] <= b_max_x[None, :])
                & (b_min_x[None, :] <= a_max_x[:, None])
                & (a_min_y[:, None] <= b_max_y[None, :])
                & (b_min_y[None, :] <= a_max_y[:, None])
            )
            broad_flat = np.flatnonzero(broad)
            if telemetry is not None:
                telemetry["broad_seconds"] = telemetry.get("broad_seconds", 0.0) + (
                    time.perf_counter() - broad_started
                )
                telemetry["broad_blocks"] = telemetry.get("broad_blocks", 0) + 1
                telemetry["max_broad_block_pairs"] = max(
                    telemetry.get("max_broad_block_pairs", 0), int(broad.size),
                )
                telemetry["broad_pairs"] = telemetry.get("broad_pairs", 0) + len(broad_flat)
                telemetry["exact_skipped_pairs"] = telemetry.get("exact_skipped_pairs", 0) + (
                    broad.size - len(broad_flat)
                )
                telemetry["empty_broad_blocks"] = telemetry.get("empty_broad_blocks", 0) + int(
                    len(broad_flat) == 0
                )
            if len(broad_flat) == 0:
                broad_started = time.perf_counter() if telemetry is not None else 0.0
                continue

            local_a = broad_flat // (b_stop - b_start)
            b_indices = b_start + broad_flat % (b_stop - b_start)
            full_flat = local_a * len(b1) + b_indices
            for pair_start in range(0, len(full_flat), max_matrix_elements):
                pair_stop = pair_start + max_matrix_elements
                pair_flat = full_flat[pair_start:pair_stop]
                pair_a = local_a[pair_start:pair_stop]
                pair_b = b_indices[pair_start:pair_stop]
                a_indices = a_start + pair_a
                exact_started = time.perf_counter() if telemetry is not None else 0.0
                cx, cy, valid = _compact_segment_intersections(
                    a1, a2, ha, b1, b2, hb, a_indices, pair_b,
                    min_angle_deg, max_angle_deg,
                )
                if telemetry is not None:
                    telemetry["exact_seconds"] = telemetry.get("exact_seconds", 0.0) + (
                        time.perf_counter() - exact_started
                    )
                    telemetry["exact_chunks"] = telemetry.get("exact_chunks", 0) + 1
                    telemetry["max_exact_chunk_pairs"] = max(
                        telemetry.get("max_exact_chunk_pairs", 0), len(pair_flat),
                    )
                    telemetry["exact_pairs"] = telemetry.get("exact_pairs", 0) + len(pair_flat)
                if np.any(valid):
                    yield a_start, cx[valid], cy[valid], pair_flat[valid]
            broad_started = time.perf_counter() if telemetry is not None else 0.0


def _compact_segment_intersections(
    a1: np.ndarray,
    a2: np.ndarray,
    ha: np.ndarray,
    b1: np.ndarray,
    b2: np.ndarray,
    hb: np.ndarray,
    a_indices: np.ndarray,
    b_indices: np.ndarray,
    min_angle_deg: float,
    max_angle_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run the historical exact formula on an AABB-compacted pair vector."""
    a1_x = a1[a_indices, 0]
    a1_y = a1[a_indices, 1]
    da_x = a2[a_indices, 0] - a1_x
    da_y = a2[a_indices, 1] - a1_y
    b1_x = b1[b_indices, 0]
    b1_y = b1[b_indices, 1]
    db_x = b2[b_indices, 0] - b1_x
    db_y = b2[b_indices, 1] - b1_y
    det = da_x * (-db_y) - (-db_x) * da_y
    parallel = np.abs(det) < 1e-12
    rhs_x = b1_x - a1_x
    rhs_y = b1_y - a1_y
    ta_num = rhs_x * (-db_y) - (-db_x) * rhs_y
    tb_num = da_x * rhs_y - rhs_x * da_y
    with np.errstate(divide="ignore", invalid="ignore"):
        ta = np.where(~parallel, ta_num / det, np.nan)
        tb = np.where(~parallel, tb_num / det, np.nan)
    angle = np.abs((hb[b_indices] - ha[a_indices] + 180.0) % 360.0 - 180.0)
    valid = (
        (~parallel)
        & (ta >= 0.0) & (ta <= 1.0)
        & (tb >= 0.0) & (tb <= 1.0)
        & (min_angle_deg <= angle) & (angle <= max_angle_deg)
    )
    return a1_x + ta * da_x, a1_y + ta * da_y, valid


def _paired_segment_intersections(
    a1: np.ndarray,
    a2: np.ndarray,
    ha: np.ndarray,
    b1: np.ndarray,
    b2: np.ndarray,
    hb: np.ndarray,
    min_angle_deg: float,
    max_angle_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Intersect explicitly paired segments without an ``N×M`` broadcast.

    All six array inputs have the same leading dimension. The returned
    indices address that paired input dimension, letting callers retain their
    own segment provenance while this helper keeps only a compact numeric
    working set.
    """
    if len(a1) == 0:
        empty_float = np.empty(0, dtype=float)
        return empty_float, empty_float, np.empty(0, dtype=int)

    angle = np.abs((hb - ha + 180.0) % 360.0 - 180.0)
    a_min_x = np.minimum(a1[:, 0], a2[:, 0])
    a_max_x = np.maximum(a1[:, 0], a2[:, 0])
    a_min_y = np.minimum(a1[:, 1], a2[:, 1])
    a_max_y = np.maximum(a1[:, 1], a2[:, 1])
    b_min_x = np.minimum(b1[:, 0], b2[:, 0])
    b_max_x = np.maximum(b1[:, 0], b2[:, 0])
    b_min_y = np.minimum(b1[:, 1], b2[:, 1])
    b_max_y = np.maximum(b1[:, 1], b2[:, 1])
    candidate = (
        (min_angle_deg <= angle) & (angle <= max_angle_deg)
        & (a_min_x <= b_max_x) & (b_min_x <= a_max_x)
        & (a_min_y <= b_max_y) & (b_min_y <= a_max_y)
    )
    input_idx = np.flatnonzero(candidate)
    if len(input_idx) == 0:
        empty_float = np.empty(0, dtype=float)
        return empty_float, empty_float, np.empty(0, dtype=int)

    aa1 = a1[input_idx]
    aa2 = a2[input_idx]
    bb1 = b1[input_idx]
    bb2 = b2[input_idx]
    da = aa2 - aa1
    db = bb2 - bb1
    det = da[:, 0] * (-db[:, 1]) - (-db[:, 0]) * da[:, 1]
    parallel = np.abs(det) < 1e-12
    rhs_x = bb1[:, 0] - aa1[:, 0]
    rhs_y = bb1[:, 1] - aa1[:, 1]
    ta_num = rhs_x * (-db[:, 1]) - (-db[:, 0]) * rhs_y
    tb_num = da[:, 0] * rhs_y - rhs_x * da[:, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ta = np.where(~parallel, ta_num / det, np.nan)
        tb = np.where(~parallel, tb_num / det, np.nan)
    valid = (~parallel) & (ta >= 0.0) & (ta <= 1.0) & (tb >= 0.0) & (tb <= 1.0)
    valid_idx = np.flatnonzero(valid)
    if len(valid_idx) == 0:
        empty_float = np.empty(0, dtype=float)
        return empty_float, empty_float, np.empty(0, dtype=int)
    return (
        aa1[valid_idx, 0] + ta[valid_idx] * da[valid_idx, 0],
        aa1[valid_idx, 1] + ta[valid_idx] * da[valid_idx, 1],
        input_idx[valid_idx],
    )


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
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return zero, one, or two ordered state-changing roots per segment.

    Args:
        a_xy: ``(n, 2)`` segment start coordinates in meters.
        b_xy: ``(n, 2)`` segment end coordinates in meters.
        center: Circle center in the same meter coordinate system.
        radius_m: Circle radius in meters.

    Returns:
        ``(root_counts, fractions, crossing_xy)``. ``root_counts`` has shape
        ``(n,)``; ``fractions`` has shape ``(n, 2)``; and ``crossing_xy`` has
        shape ``(n, 2, 2)``. Empty slots are ``nan``. Tangencies are omitted
        because touching a circle without changing inside/outside state is not
        a crossing.
    """
    a = np.asarray(a_xy, dtype=float)
    b = np.asarray(b_xy, dtype=float)
    if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 2:
        raise ValueError("a_xy and b_xy must have matching shape (n, 2)")

    n = a.shape[0]
    if n == 0:
        return (
            np.zeros(0, dtype=np.int8),
            np.empty((0, 2), dtype=float),
            np.empty((0, 2, 2), dtype=float),
        )

    center_xy = np.asarray(center, dtype=float)
    rel_a = a - center_xy
    rel_b = b - center_xy
    delta = rel_b - rel_a

    qa = np.einsum("ij,ij->i", delta, delta)
    qb = 2.0 * np.einsum("ij,ij->i", rel_a, delta)
    qc = np.einsum("ij,ij->i", rel_a, rel_a) - radius_m * radius_m
    disc = qb * qb - 4.0 * qa * qc

    # A zero discriminant is a tangent: it does not toggle circle membership.
    tolerance = np.finfo(float).eps * np.maximum(
        1.0,
        np.abs(qb * qb) + np.abs(4.0 * qa * qc),
    ) * 16.0
    valid = (qa > 0.0) & (disc > tolerance)
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
    fractions = np.full((n, 2), np.nan, dtype=float)
    fractions[:, 0] = np.where(t1_inside, t1, np.where(t2_inside, t2, np.nan))
    fractions[:, 1] = np.where(t1_inside & t2_inside, t2, np.nan)
    root_counts = np.sum(np.isfinite(fractions), axis=1, dtype=np.int8)

    crossing_xy = np.full((n, 2, 2), np.nan, dtype=float)
    for root_index in (0, 1):
        hit = np.isfinite(fractions[:, root_index])
        crossing_xy[hit, root_index] = (
            a[hit] + fractions[hit, root_index, None] * delta[hit]
        )
    return root_counts, fractions, crossing_xy


# ── Branch angle relative to center ───────────────────────────────────


def _branch_angle_from_center(
    center: tuple[float, float], boundary_point: tuple[float, float]
) -> float:
    """Bearing from center to boundary point, representing a branch direction."""
    return _bearing_deg(center, boundary_point)
