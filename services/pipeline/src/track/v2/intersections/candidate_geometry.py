"""Candidate geometry in the processing CRS (metres), with equal event weights."""
from __future__ import annotations

import logging
import math

import numpy as np

from ..exceptions import V2Error


_LOGGER = logging.getLogger(__name__)
_MEAN_DISTANCE_GAP_M = 1.0
_FAST_ITERATIONS = 256
_MAX_ITERATIONS = 100_000


class CandidateGeometryNumericalError(V2Error):
    """A candidate could not satisfy the mean-distance optimality criterion."""

    def __init__(self, reason: str, **diagnostics: float | int) -> None:
        self.diagnostics = diagnostics
        details = ", ".join(f"{key}={value}" for key, value in diagnostics.items())
        super().__init__(f"candidate geometric median numerical failure: {reason} ({details})")


def _optimality_gap(
    offsets: np.ndarray, center: np.ndarray, output_error: float = 0.0,
) -> tuple[float, float]:
    """Bound F(center)-min(F), where F is the mean Euclidean distance.

    An optimum lies in the members' convex hull. A (possibly approximate)
    subgradient g therefore gives the bound max_i g dot (center-p_i).
    At coincident members, choose their subgradients to cancel as much of the
    other members' gradient as possible. Treat distances below roundoff as
    coincident, charging twice their mean distance for this approximation.
    Extended precision and a conservative roundoff allowance prevent accepting
    a marginal certificate solely because of floating-point cancellation.
    """
    delta = np.asarray(center, dtype=np.longdouble) - offsets.astype(np.longdouble)
    distances = np.hypot(delta[:, 0], delta[:, 1])
    scale = float(np.max(distances))
    near = distances <= 16 * np.finfo(np.float64).eps * scale
    far = ~near
    residual = np.sum(delta[far] / distances[far, None], axis=0)
    residual_norm = np.hypot(*residual)
    coincident = int(np.count_nonzero(near))
    if residual_norm <= coincident:
        gradient = np.zeros(2, dtype=np.longdouble)
    else:
        gradient = residual * ((1 - coincident / residual_norm) / len(offsets))
    support = np.max(delta @ gradient)
    near_error = 2 * np.sum(distances[near]) / len(offsets)
    roundoff = (128 * np.finfo(np.float64).eps * scale
                * (1 + math.log2(len(offsets) + 1)))
    gap = max(0.0, float(support)) + float(near_error) + roundoff + output_error
    return gap, float(np.mean(distances))


def candidate_circle(xy: np.ndarray) -> tuple[np.ndarray, float]:
    """Return an accepted geometric median and inverse empirical 95% radius.

    Every event retains equal weight, including repeated coordinates. The
    returned centre has a verified mean-distance objective gap of at most one
    metre; this criterion does not assert a one-metre centre position error.
    Modified Weiszfeld updates usually achieve the earlier micrometre movement
    target. Slow or nonunique solutions can stop once the objective certificate
    passes. Exhausting the safety limit without a certificate fails explicitly.
    Radius trimming does not remove any members from the candidate cluster.
    """
    points = np.asarray(xy, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) == 0:
        raise ValueError("candidate geometry requires a nonempty (n, 2) array")
    if not np.isfinite(points).all():
        raise ValueError("candidate geometry requires finite coordinates")

    # The local origin avoids summing projected UTM offsets. Use long double
    # subtraction first so unrepresentable spans become explicit failures.
    origin = np.asarray(np.median(points.astype(np.longdouble), axis=0), dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        offsets = np.asarray(points.astype(np.longdouble) - origin, dtype=np.float64)
    if not np.isfinite(offsets).all():
        raise CandidateGeometryNumericalError("coordinate span overflow", point_count=len(points))
    center = np.zeros(2, dtype=np.float64)
    gap = math.inf
    objective = math.inf
    for iteration in range(_MAX_ITERATIONS + 1):
        delta = offsets - center
        distances = np.hypot(delta[:, 0], delta[:, 1])
        nonzero = distances > 0
        coincident = len(points) - np.count_nonzero(nonzero)
        residual = np.sum(delta[nonzero] / distances[nonzero, None], axis=0,
                          dtype=np.longdouble)
        residual_norm = np.hypot(*residual)
        if residual_norm <= coincident:
            step = np.zeros(2)
        else:
            # Scale inverse-distance weights to avoid overflow near a member.
            minimum = np.min(distances[nonzero])
            denominator = np.sum(minimum / distances[nonzero], dtype=np.longdouble)
            step = np.asarray(residual * (minimum / denominator)
                              * (1 - coincident / residual_norm), dtype=np.float64)
        step_norm = float(np.hypot(*step))
        updated = center + step
        stagnated = np.array_equal(updated, center)
        if step_norm <= 1e-6 or stagnated or iteration >= _FAST_ITERATIONS:
            output = origin + center
            output_error = float(np.hypot(*(output - origin - center)))
            gap, objective = _optimality_gap(offsets, center, output_error)
            if np.isfinite(output).all() and math.isfinite(gap) and gap <= _MEAN_DISTANCE_GAP_M:
                final_delta = points - output
                final_distances = np.hypot(final_delta[:, 0], final_delta[:, 1])
                # Exact inverse empirical CDF rank, without interpolation.
                rank = (95 * len(points) + 99) // 100 - 1
                radius = float(np.partition(final_distances, rank)[rank])
                if math.isfinite(radius):
                    _LOGGER.debug("candidate median accepted: points=%d iterations=%d "
                                  "mean_distance_m=%.9g objective_gap_bound_m=%.9g",
                                  len(points), iteration, objective, gap)
                    return output, radius
                raise CandidateGeometryNumericalError(
                    "radius overflow", point_count=len(points), iterations=iteration,
                    mean_distance_m=objective, objective_gap_bound_m=gap,
                    tolerance_m=_MEAN_DISTANCE_GAP_M,
                )
        if iteration == _MAX_ITERATIONS or not np.isfinite(step).all():
            break
        if stagnated:
            break
        center = updated
    raise CandidateGeometryNumericalError(
        "optimality criterion not satisfied", point_count=len(points),
        iterations=iteration, mean_distance_m=objective,
        objective_gap_bound_m=gap, tolerance_m=_MEAN_DISTANCE_GAP_M,
    )
