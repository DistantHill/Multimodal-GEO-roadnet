"""Network stage — assemble intersection nodes and centerline edges."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .intersect2 import IntersectResult
from .models import (
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
    intersections: IntersectResult | Iterable[ClipIntersection],
    *,
    config: NetworkConfig | None = None,
) -> NetworkResult:
    """Build an undirected physical-road graph from fitted centerlines."""
    cfg = config or NetworkConfig()
    intersections_by_id = _intersection_map(intersections)
    degrees = {cluster_id: 0 for cluster_id in intersections_by_id}
    edges: list[NetworkEdge] = []
    issues: list[NetworkIssue] = []
    seen_pairs: set[tuple[int, int]] = set()

    for centerline in fits.centerlines:
        pair = (centerline.from_cluster_id, centerline.to_cluster_id)
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
                from_cluster_id=pair[0],
                to_cluster_id=pair[1],
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
            cluster_id=intersection.cluster_id,
            lon=intersection.lon,
            lat=intersection.lat,
            radius_m=intersection.radius_m,
            degree=degrees[intersection.cluster_id],
        )
        for intersection in sorted(intersections_by_id.values(), key=lambda item: item.cluster_id)
        if cfg.include_isolated_nodes or degrees[intersection.cluster_id] > 0
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
        run_id=fits.run_id,
    )


def _intersection_map(
    intersections: IntersectResult | Iterable[ClipIntersection],
) -> dict[int, ClipIntersection]:
    if isinstance(intersections, IntersectResult):
        clusters = {cluster.cluster_id: cluster for cluster in intersections.clusters}
        values: list[ClipIntersection] = []
        for topology in intersections.topologies:
            if not topology.is_complete:
                continue
            cluster = clusters.get(topology.cluster_id)
            if cluster is None:
                raise ValueError(
                    f"complete topology {topology.cluster_id} has no matching cluster"
                )
            radius_m = topology.clip_radius_m if topology.clip_radius_m > 0.0 else cluster.radius_m
            values.append(
                ClipIntersection(
                    cluster_id=cluster.cluster_id,
                    lon=cluster.centroid_lon,
                    lat=cluster.centroid_lat,
                    radius_m=radius_m,
                )
            )
    else:
        values = list(intersections)

    result: dict[int, ClipIntersection] = {}
    for intersection in values:
        if intersection.cluster_id in result:
            raise ValueError(f"duplicate intersection cluster_id: {intersection.cluster_id}")
        if not (
            math.isfinite(intersection.lon)
            and math.isfinite(intersection.lat)
            and math.isfinite(intersection.radius_m)
            and intersection.radius_m > 0.0
        ):
            raise ValueError(f"intersection {intersection.cluster_id} has invalid geometry")
        result[intersection.cluster_id] = intersection
    return result
