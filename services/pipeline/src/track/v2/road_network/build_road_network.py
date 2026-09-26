"""Network stage — assemble intersection nodes and centerline edges."""

from __future__ import annotations

import math
from collections.abc import Iterable

from ..models import (
    ClipIntersection,
    FitResult,
    NetworkConfig,
    NetworkEdge,
    NetworkIssue,
    NetworkNode,
    NetworkReport,
    NetworkResult,
)


def build_network(
    fits: FitResult,
    intersections: Iterable[ClipIntersection],
    *,
    config: NetworkConfig,
) -> NetworkResult:
    """Build an undirected physical-road graph from fitted centerlines."""
    cfg = config
    intersections_by_id = _intersection_map(intersections)
    degrees = {intersection_id: 0 for intersection_id in intersections_by_id}
    edges: list[NetworkEdge] = []
    issues: list[NetworkIssue] = []
    seen_pairs: set[tuple[int, int]] = set()

    for centerline in fits.centerlines:
        if centerline.variant != "combined":
            raise ValueError("Network requires combined centerlines")
        pair = (
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )
        if pair[0] >= pair[1]:
            raise ValueError(
                f"centerline {pair[0]}--{pair[1]} must use canonical direction"
            )
        if pair in seen_pairs:
            raise ValueError(f"duplicate centerline pair: {pair[0]}--{pair[1]}")
        seen_pairs.add(pair)
        if pair[0] not in intersections_by_id or pair[1] not in intersections_by_id:
            issues.append(NetworkIssue(pair[0], pair[1], "missing_intersection"))
            continue
        if (
            len(centerline.points) < 2
            or not math.isfinite(centerline.length_m)
            or centerline.length_m <= 0.0
            or any(
                not (math.isfinite(lon) and math.isfinite(lat))
                for lon, lat in centerline.points
            )
        ):
            issues.append(NetworkIssue(pair[0], pair[1], "invalid_centerline_geometry"))
            continue
        edges.append(
            NetworkEdge(
                edge_id=f"{pair[0]}--{pair[1]}",
                from_intersection_id=pair[0],
                to_intersection_id=pair[1],
                points=centerline.points,
                length_m=centerline.length_m,
                source_clip_count=centerline.source_clip_count,
                forward_clip_count=centerline.forward_clip_count,
                reverse_clip_count=centerline.reverse_clip_count,
            )
        )
        degrees[pair[0]] += 1
        degrees[pair[1]] += 1

    isolated_count = sum(degree == 0 for degree in degrees.values())
    nodes = tuple(
        NetworkNode(
            intersection_id=intersection.intersection_id,
            lon=intersection.lon,
            lat=intersection.lat,
            radius_m=intersection.radius_m,
            degree=degrees[intersection.intersection_id],
        )
        for intersection in sorted(
            intersections_by_id.values(),
            key=lambda item: item.intersection_id,
        )
        if cfg.include_isolated_nodes or degrees[intersection.intersection_id] > 0
    )
    return NetworkResult(
        nodes=nodes,
        edges=tuple(edges),
        issues=tuple(issues),
        report=NetworkReport(
            input_centerline_count=len(fits.centerlines),
            node_count=len(nodes),
            edge_count=len(edges),
            isolated_node_count=isolated_count,
            skipped_centerline_count=len(issues),
        ),
        detect_id=fits.detect_id,
    )


def _intersection_map(
    intersections: Iterable[ClipIntersection],
) -> dict[int, ClipIntersection]:
    result: dict[int, ClipIntersection] = {}
    for intersection in intersections:
        if intersection.intersection_id in result:
            raise ValueError(
                f"duplicate intersection_id: {intersection.intersection_id}"
            )
        if not (
            math.isfinite(intersection.lon)
            and math.isfinite(intersection.lat)
            and math.isfinite(intersection.radius_m)
            and intersection.radius_m > 0.0
        ):
            raise ValueError(
                f"intersection {intersection.intersection_id} has invalid geometry"
            )
        result[intersection.intersection_id] = intersection
    return result
