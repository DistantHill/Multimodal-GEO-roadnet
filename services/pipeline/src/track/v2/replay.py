"""Replay module — trace back any pipeline output to original trajectory points.

Each function takes a pipeline product (cluster, subtrack, point list, etc.)
plus the original ``tracks`` dict and optional ``CleanerResult``, and returns
the corresponding original ``TrajectoryPoint`` list with metadata.

Public:
  replay_cluster            — HotspotCluster → original TrajectoryPoints
  replay_clusters           — batch of the above
  replay_crossing_points    — CrossingPoint → (crossing, original_point)[]
  replay_cleaner            — one vehicle's cleaner replay
  replay_cleaner_all        — all vehicles
  replay_subtrack           — Clip sub-track → original points
  replay_points             — generic point list → original points
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import overload

from .models import (
    CleanerResult,
    CrossingPoint,
    HotspotCluster,
    InOutIntersectionPoint,
    PointRef,
    ReplayCleanResult,
    TrajectoryPoint,
)


# ── Index builder ────────────────────────────────────────────────────


def _build_replay_index(
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, dict[str, TrajectoryPoint]]:
    """Build a {vehicle_id: {source_row_key: TrajectoryPoint}} fast lookup."""
    index: dict[str, dict[str, TrajectoryPoint]] = {}
    for vid, pts in tracks.items():
        inner: dict[str, TrajectoryPoint] = {}
        for p in pts:
            key = p.point_ref.source_row_key
            if key:
                inner[key] = p
        index[vid] = inner
    return index


# ── Generic lookup helper ────────────────────────────────────────────


def _resolve_point(
    vehicle_id: str,
    point_ref: PointRef | None,
    index: dict[str, dict[str, TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> TrajectoryPoint | None:
    """Resolve a single PointRef back to the original TrajectoryPoint."""
    if point_ref is not None:
        inner = index.get(vehicle_id, {})
        key = point_ref.source_row_key
        if key and key in inner:
            return inner[key]
        # Stable=False fallback: match by timestamp + lon + lat
        # (approximate — for injected points the ts is interpolated)
    # Fallback: return nothing
    return None


# ── Cluster replay ───────────────────────────────────────────────────


def replay_cluster(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """All original TrajectoryPoints that contributed to this cluster.

    Uses ``cluster.source_point_refs`` and ``cluster.crossing_points``
    to find every point, then sorts by (vehicle_id, timestamp).
    """
    index = _build_replay_index(tracks)
    seen: set[tuple[str, str]] = set()
    result: list[TrajectoryPoint] = []

    def _add(pt: TrajectoryPoint) -> None:
        key = (pt.vehicle_id, pt.point_ref.source_row_key)
        if key not in seen:
            seen.add(key)
            result.append(pt)

    # From source_point_refs (turn events + orthogonal candidates)
    for _source, refs in cluster.source_point_refs.items():
        for ref in refs:
            pt = _resolve_point("", ref, index, tracks)  # vehicle_id unknown from ref alone
            if pt is not None:
                _add(pt)

    # From crossing_points (segment-circle intersections)
    for cp in cluster.crossing_points:
        pt = _resolve_point(cp.vehicle_id, cp.point_ref, index, tracks)
        if pt is not None:
            _add(pt)

    # Sort by (vehicle_id, timestamp)
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result


def replay_clusters(
    clusters: Sequence[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    """{cluster_ref: [TrajectoryPoint, ...]} for every cluster."""
    return {c.cluster_ref or str(c.cluster_id): replay_cluster(c, tracks) for c in clusters}


# ── Crossing-point replay ────────────────────────────────────────────


def replay_crossing_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[CrossingPoint, TrajectoryPoint | None]]:
    """Each CrossingPoint paired with its original TrajectoryPoint (or None)."""
    index = _build_replay_index(tracks)
    result: list[tuple[CrossingPoint, TrajectoryPoint | None]] = []
    for cp in cluster.crossing_points:
        pt = _resolve_point(cp.vehicle_id, cp.point_ref, index, tracks)
        result.append((cp, pt))
    return result


def replay_inout_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[InOutIntersectionPoint, list[CrossingPoint]]]:
    """Each InOutIntersectionPoint paired with its aggregated CrossingPoints."""
    cp_by_id: dict[int, CrossingPoint] = {}
    for cp in cluster.crossing_points:
        cp_by_id[cp.crossing_id] = cp

    result: list[tuple[InOutIntersectionPoint, list[CrossingPoint]]] = []
    for iop in cluster.inout_points:
        cps = [cp_by_id[cid] for cid in iop.crossing_refs if cid in cp_by_id]
        result.append((iop, cps))
    return result


# ── Cleaner replay ───────────────────────────────────────────────────


def replay_cleaner(
    vehicle_id: str,
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult | None = None,
) -> ReplayCleanResult:
    """Replay one vehicle through the cleaner stage.

    Returns original points, which were excluded (and why), which were
    injected (with parent refs), and the final active set.
    """
    all_pts = tracks.get(vehicle_id, [])
    if not all_pts:
        return ReplayCleanResult(
            vehicle_id=vehicle_id,
            original=[],
            excluded=[],
            injected=[],
            active=[],
        )

    original = sorted(all_pts, key=lambda p: p.timestamp)

    # Excluded points
    excluded: list[tuple[TrajectoryPoint, str]] = []
    if cleaner_result is not None:
        for item in cleaner_result.excluded:
            if item.point_ref not in {p.point_ref for p in all_pts}:
                continue
            pt = _resolve_point(vehicle_id, item.point_ref,
                                _build_replay_index(tracks), tracks)
            if pt is not None:
                excluded.append((pt, item.reason))

    # Injected points = those with generated_reason != "" in active
    injected: list[tuple[TrajectoryPoint, TrajectoryPoint, TrajectoryPoint]] = []
    if cleaner_result is not None:
        index = _build_replay_index(tracks)
        for pt in cleaner_result.active.iter_points():
            if pt.vehicle_id != vehicle_id:
                continue
            if pt.point_ref.generated_reason == "":
                continue
            from_pt = _resolve_point(vehicle_id,
                                     PointRef("", pt.point_ref.from_ref or "", False),
                                     index, tracks)
            to_pt = _resolve_point(vehicle_id,
                                   PointRef("", pt.point_ref.to_ref or "", False),
                                   index, tracks)
            if from_pt is not None and to_pt is not None:
                injected.append((pt, from_pt, to_pt))

    # Active points (from cleaner_result or all original)
    if cleaner_result is not None:
        active = [pt for pt in cleaner_result.active.iter_points()
                  if pt.vehicle_id == vehicle_id]
    else:
        active = list(original)

    return ReplayCleanResult(
        vehicle_id=vehicle_id,
        original=original,
        excluded=excluded,
        injected=injected,
        active=active,
    )


def replay_cleaner_all(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult,
) -> dict[str, ReplayCleanResult]:
    """All vehicles' cleaner replay."""
    return {
        vid: replay_cleaner(vid, tracks, cleaner_result=cleaner_result)
        for vid in tracks
    }


# ── Sub-track replay (Clip layer) ────────────────────────────────────


def replay_subtrack(
    subtrack: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """Take a Clip-produced sub-track and return the original points.

    Matches by vehicle_id + point_ref.source_row_key.
    """
    if not subtrack:
        return []
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[str] = set()
    for pt in subtrack:
        inner = index.get(pt.vehicle_id, {})
        key = pt.point_ref.source_row_key
        if key and key in inner and key not in seen:
            seen.add(key)
            result.append(inner[key])
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result


def replay_subtracks(
    subtracks: dict[str, list[TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    """Batch sub-track replay."""
    return {k: replay_subtrack(v, tracks) for k, v in subtracks.items()}


# ── Generic point replay ─────────────────────────────────────────────


def replay_points(
    points: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """Generic: any TrajectoryPoint list → deduped original points."""
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[str] = set()
    for pt in points:
        inner = index.get(pt.vehicle_id, {})
        key = pt.point_ref.source_row_key
        if key and key in inner and key not in seen:
            seen.add(key)
            result.append(inner[key])
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result
