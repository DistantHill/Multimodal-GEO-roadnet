"""Resolve persisted derived-point references to their source trajectories."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import (
    CrossingPoint,
    HotspotCluster,
    InOutIntersectionPoint,
    PointRef,
    RefineResult,
    ReplayRefineResult,
    TrajectoryPoint,
)

def _build_replay_index(
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, dict[tuple[str, str], TrajectoryPoint]]:
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]] = {}
    for vehicle_id, points in tracks.items():
        index[vehicle_id] = {
            (point.point_ref.source_id, point.point_ref.source_row_key): point
            for point in points
            if point.point_ref.source_id and point.point_ref.source_row_key
        }
    return index

def _resolve_point(
    vehicle_id: str,
    point_ref: PointRef | None,
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> TrajectoryPoint | None:
    del tracks
    if point_ref is None:
        return None
    key = (point_ref.source_id, point_ref.source_row_key)
    if vehicle_id:
        return index.get(vehicle_id, {}).get(key)
    matches = [inner[key] for inner in index.values() if key in inner]
    return matches[0] if len(matches) == 1 else None

def replay_cluster(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    index = _build_replay_index(tracks)
    seen: set[tuple[str, str]] = set()
    result: list[TrajectoryPoint] = []

    def add(point: TrajectoryPoint) -> None:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        if key not in seen:
            seen.add(key)
            result.append(point)

    for refs in cluster.source_point_refs.values():
        for ref in refs:
            point = _resolve_point("", ref, index, tracks)
            if point is not None:
                add(point)
    for crossing in cluster.crossing_points:
        point = _resolve_point(crossing.vehicle_id, crossing.point_ref, index, tracks)
        if point is not None:
            add(point)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result

def replay_clusters(
    clusters: Sequence[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    return {
        cluster.cluster_ref or str(cluster.cluster_calculate_index): replay_cluster(
            cluster, tracks
        )
        for cluster in clusters
    }

def replay_crossing_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[CrossingPoint, TrajectoryPoint | None]]:
    index = _build_replay_index(tracks)
    return [
        (
            crossing,
            _resolve_point(crossing.vehicle_id, crossing.point_ref, index, tracks),
        )
        for crossing in cluster.crossing_points
    ]

def replay_inout_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[InOutIntersectionPoint, list[CrossingPoint]]]:
    del tracks
    crossings = {
        crossing.crossing_calculate_index: crossing
        for crossing in cluster.crossing_points
    }
    return [
        (
            point,
            [crossings[key] for key in point.crossing_refs if key in crossings],
        )
        for point in cluster.inout_points
    ]

def replay_refine(
    vehicle_id: str,
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    refine_result: RefineResult | None = None,
) -> ReplayRefineResult:
    all_points = tracks.get(vehicle_id, [])
    if not all_points:
        return ReplayRefineResult(vehicle_id, [], [], [], [])
    original = sorted(all_points, key=lambda point: point.timestamp)
    index = _build_replay_index(tracks)
    excluded: list[tuple[TrajectoryPoint, str]] = []
    if refine_result is not None:
        original_refs = {point.point_ref for point in all_points}
        for item in refine_result.excluded:
            if item.point_ref not in original_refs:
                continue
            point = _resolve_point(vehicle_id, item.point_ref, index, tracks)
            if point is not None:
                excluded.append((point, item.reason))
    injected: list[tuple[TrajectoryPoint, TrajectoryPoint, TrajectoryPoint]] = []
    if refine_result is not None:
        for point in refine_result.active.iter_points():
            if point.vehicle_id != vehicle_id or not point.point_ref.generated_reason:
                continue
            left = _resolve_point(
                vehicle_id,
                PointRef(
                    point.point_ref.source_id,
                    point.point_ref.from_ref or "",
                    False,
                ),
                index,
                tracks,
            )
            right = _resolve_point(
                vehicle_id,
                PointRef(
                    point.point_ref.source_id,
                    point.point_ref.to_ref or "",
                    False,
                ),
                index,
                tracks,
            )
            if left is not None and right is not None:
                injected.append((point, left, right))
    active = (
        [
            point
            for point in refine_result.active.iter_points()
            if point.vehicle_id == vehicle_id
        ]
        if refine_result is not None
        else list(original)
    )
    return ReplayRefineResult(vehicle_id, original, excluded, injected, active)

def replay_refine_all(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    refine_result: RefineResult,
) -> dict[str, ReplayRefineResult]:
    return {
        vehicle_id: replay_refine(
            vehicle_id, tracks, refine_result=refine_result
        )
        for vehicle_id in tracks
    }

def replay_subtrack(
    subtrack: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    if not subtrack:
        return []
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in subtrack:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        original = index.get(point.vehicle_id, {}).get(key)
        if original is not None and key not in seen:
            seen.add(key)
            result.append(original)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result

def replay_subtracks(
    subtracks: dict[str, list[TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    return {
        key: replay_subtrack(points, tracks) for key, points in subtracks.items()
    }

def replay_points(
    points: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in points:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        original = index.get(point.vehicle_id, {}).get(key)
        if original is not None and key not in seen:
            seen.add(key)
            result.append(original)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result
