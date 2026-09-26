"""Extract verified intersection topology from detected clusters and tracks."""
from __future__ import annotations

import math
import time
from bisect import bisect_right
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .detect_intersections import IntersectConfig
    from ..trajectories.results.read_parquet import RefineDatasetReader

from ..telemetry.console_logging import log_info
from ..telemetry.heartbeat import heartbeat_progress, observed_operation
from .performance_timing import PerfTracker
from .progress import report_detect_progress
from .._geometry import (
    _angle_delta_deg,
    _branch_angle_from_center,
    _circular_mean_deg,
    _point_xy,
    _segment_bearings,
    _segment_circle_crossings_batch,
)
from ..models import (
    CrossingPoint,
    EntryExitObservation,
    HotspotCluster,
    IntersectionTopology,
    TrajectoryPoint,
)
from ..projection import CoordinateTransformer, SpatialWindow


def _perf_iter(perf, iterable, *, name, total=None, track=True):
    if perf is not None:
        return perf.timed_iter(iterable, name=name, total=total, track=track)
    return iterable


def _topology_point_windows(
    clusters: list[HotspotCluster],
    cfg: IntersectConfig,
) -> list[SpatialWindow]:
    """Return processing-CRS query boxes for in-memory topology routing."""
    windows: list[SpatialWindow] = []
    for cluster in clusters:
        topology_radius_m = max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        )
        # Keep parity with the former per-cluster query, which deliberately
        # fetched a 2x-radius point halo before testing the exact circle.
        query_radius_m = topology_radius_m * 2.0
        windows.append(SpatialWindow(
            x_min=cluster.centroid_x - query_radius_m,
            y_min=cluster.centroid_y - query_radius_m,
            x_max=cluster.centroid_x + query_radius_m,
            y_max=cluster.centroid_y + query_radius_m,
        ))
    return windows


@dataclass
class _TopologyPointIndex:
    """One-dimensional interval index over real adjacent track segments.

    A point-only window query loses a segment whenever one or both endpoints
    sit outside the query box.  It can also join different subtracks after
    grouping the selected points by ``vehicle_id``.  Instead, retain the
    caller's track key and route a complete subtrack when any of its real
    adjacent segment AABBs intersects the query window.  The complete subtrack
    is required to initialise circle membership from its true first point and
    to preserve the full bearing context around a crossing.
    """

    axis: str
    lower_bounds: list[float]
    prefix_upper_bounds: list[float]
    segments: list[tuple[str, float, float, float, float]]
    tracks: dict[str, list[TrajectoryPoint]]

    def tracks_in(self, window: SpatialWindow) -> dict[str, list[TrajectoryPoint]]:
        """Return complete subtracks with a segment AABB touching ``window``."""
        if self.axis == "x":
            lower, upper = window.x_min, window.x_max
        else:
            lower, upper = window.y_min, window.y_max
        stop = bisect_right(self.lower_bounds, upper)

        selected_track_keys: set[str] = set()
        segment_index = stop - 1
        while segment_index >= 0:
            # No interval at or before this position can reach the window.
            if self.prefix_upper_bounds[segment_index] < lower:
                break
            track_key, min_x, min_y, max_x, max_y = self.segments[segment_index]
            if (
                min_x <= window.x_max
                and max_x >= window.x_min
                and min_y <= window.y_max
                and max_y >= window.y_min
            ):
                selected_track_keys.add(track_key)
            segment_index -= 1

        # Preserve the original, complete subtrack and its ordering.  In
        # particular, do not key this result by vehicle_id: one vehicle may
        # own several independent subtracks.
        return {
            track_key: self.tracks[track_key]
            for track_key in sorted(selected_track_keys)
        }


def _build_topology_point_index(
    tracks: dict[str, list[TrajectoryPoint]],
) -> _TopologyPointIndex:
    """Index real adjacent segment AABBs once for per-window topology routing.

    Segments are sorted by their lower bound on the wider dataset axis.  A
    prefix maximum of upper bounds lets a backwards query stop as soon as no
    earlier interval can overlap.  Each segment is stored once and no
    projected geometry or circle fraction is persisted.
    """
    point_coordinates = [
        _point_xy(point)
        for track in tracks.values()
        for point in track
    ]
    if not point_coordinates:
        return _TopologyPointIndex(
            axis="x",
            lower_bounds=[],
            prefix_upper_bounds=[],
            segments=[],
            tracks=tracks,
        )
    axis = (
        "x"
        if (max(x for x, _ in point_coordinates) - min(x for x, _ in point_coordinates)
            >= max(y for _, y in point_coordinates) - min(y for _, y in point_coordinates))
        else "y"
    )

    segments: list[tuple[str, float, float, float, float]] = []
    for track_key, track in tracks.items():
        for start, end in zip(track, track[1:]):
            start_x, start_y = _point_xy(start)
            end_x, end_y = _point_xy(end)
            segments.append((
                track_key,
                min(start_x, end_x),
                min(start_y, end_y),
                max(start_x, end_x),
                max(start_y, end_y),
            ))
    axis_min_index = 1 if axis == "x" else 2
    axis_max_index = 3 if axis == "x" else 4
    segments.sort(key=lambda segment: segment[axis_min_index])
    lower_bounds = [segment[axis_min_index] for segment in segments]
    prefix_upper_bounds: list[float] = []
    prefix_upper = -math.inf
    for segment in segments:
        prefix_upper = max(prefix_upper, segment[axis_max_index])
        prefix_upper_bounds.append(prefix_upper)
    return _TopologyPointIndex(
        axis=axis,
        lower_bounds=lower_bounds,
        prefix_upper_bounds=prefix_upper_bounds,
        segments=segments,
        tracks=tracks,
    )


@observed_operation("拓扑候选验证")
def _build_topologies_from_tracks(
    clusters: list[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
    refine_reader: RefineDatasetReader | None = None,
) -> tuple[list[IntersectionTopology], list[HotspotCluster], list[EntryExitObservation]]:
    """Verify topologies from real adjacent segments and complete subtracks.

    DB runs query the temporary disk index and process one complete subtrack at
    a time; handle/reference callers retain their existing in-memory index.
    """
    total_clusters = len(clusters)
    heartbeat_progress(0, total=total_clusters, unit="候选交叉口")
    log_info(f"── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")
    t0 = time.perf_counter()
    windows = _topology_point_windows(clusters, cfg)
    if refine_reader is not None:
        return _build_topologies_from_reader(
            clusters, windows, refine_reader, cfg, transformer,
            live_status=live_status, perf=perf, started_at=t0,
        )
    if perf is None:
        point_index = _build_topology_point_index(tracks)
    else:
        with perf.stage("topology_index_points"):
            point_index = _build_topology_point_index(tracks)

    topologies: list[IntersectionTopology] = []
    annotated_clusters: list[HotspotCluster] = []
    all_observations: list[EntryExitObservation] = []
    for cluster_index, cluster in enumerate(
        _perf_iter(perf, clusters, name="topo_clusters", total=total_clusters)
    ):
        report_detect_progress(
            live_status,
            detail=f"簇/Cluster:\t{cluster_index + 1:,}/{total_clusters:,}",
            progress=(cluster_index + 1) / total_clusters,
            elapsed=time.perf_counter() - t0,
            stage="Detect: 拓扑验证 / Topology",
        )
        radius = max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        )
        if perf is None:
            nearby_tracks = point_index.tracks_in(windows[cluster_index])
        else:
            with perf.stage("topology_route_window"):
                nearby_tracks = point_index.tracks_in(windows[cluster_index])
        observations, crossing_points = _extract_entry_exit_observations_batch(
            nearby_tracks,
            cluster=cluster,
            transformer=transformer,
            radius_m=radius,
            bearing_window=cfg.bearing_window,
        )
        cluster = replace(cluster, crossing_points=tuple(crossing_points))
        topology, annotated, cluster_observations = _build_topology_for_cluster(
            cluster, observations, cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_observations)
        heartbeat_progress(cluster_index + 1, total=total_clusters, unit="候选交叉口")

    log_info(
        f"── 拓扑验证: {len(topologies):,} 拓扑/topologies "
        f"({time.perf_counter() - t0:.1f}s)"
    )
    return topologies, annotated_clusters, all_observations


def _build_topologies_from_reader(
    clusters: list[HotspotCluster],
    windows: list[SpatialWindow],
    reader: RefineDatasetReader,
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
    *,
    live_status: Any,
    perf: PerfTracker | None,
    started_at: float,
) -> tuple[list[IntersectionTopology], list[HotspotCluster], list[EntryExitObservation]]:
    """Route complete reader subtracks without constructing a global track map."""
    observations_by_cluster: list[list[EntryExitObservation]] = [[] for _ in clusters]
    crossings_by_cluster: list[list[CrossingPoint]] = [[] for _ in clusters]
    with closing(reader.iter_topology_tracks(
        cfg.source_table or cfg.db_table, windows, batch_size=cfg.stream_batch_size,
    )) as selections:
        for selection in selections:
            for cluster_index in selection.window_indices:
                cluster = clusters[cluster_index]
                radius = max(
                    cfg.min_intersection_radius_m,
                    cluster.radius_m + cfg.topology_radius_padding_m,
                )
                observations, crossings = _extract_entry_exit_observations_batch(
                    {selection.track_key: selection.points}, cluster=cluster,
                    transformer=transformer, radius_m=radius,
                    bearing_window=cfg.bearing_window,
                )
                target_crossings = crossings_by_cluster[cluster_index]
                offset = len(target_crossings)
                target_crossings.extend(
                    replace(point, crossing_calculate_index=offset + point.crossing_calculate_index)
                    for point in crossings
                )
                observations_by_cluster[cluster_index].extend(replace(
                    item,
                    entry_crossing_calculate_index=(
                        None if item.entry_crossing_calculate_index is None
                        else offset + item.entry_crossing_calculate_index
                    ),
                    exit_crossing_calculate_index=(
                        None if item.exit_crossing_calculate_index is None
                        else offset + item.exit_crossing_calculate_index
                    ),
                ) for item in observations)

    topologies: list[IntersectionTopology] = []
    annotated_clusters: list[HotspotCluster] = []
    all_observations: list[EntryExitObservation] = []
    total = len(clusters)
    for cluster_index, original in enumerate(
        _perf_iter(perf, clusters, name="topo_clusters", total=total)
    ):
        report_detect_progress(
            live_status,
            detail=f"簇/Cluster:\t{cluster_index + 1:,}/{total:,}",
            progress=(cluster_index + 1) / total,
            elapsed=time.perf_counter() - started_at,
            stage="Detect: 拓扑验证 / Topology",
        )
        cluster = replace(
            original, crossing_points=tuple(crossings_by_cluster[cluster_index]),
        )
        topology, annotated, cluster_observations = _build_topology_for_cluster(
            cluster, observations_by_cluster[cluster_index], cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_observations)
        heartbeat_progress(cluster_index + 1, total=total, unit="候选交叉口")
    log_info(
        f"── 拓扑验证: {len(topologies):,} 拓扑/topologies "
        f"({time.perf_counter() - started_at:.1f}s)"
    )
    return topologies, annotated_clusters, all_observations





def _cluster_angles_greedy(
    angles: list[float], threshold_deg: float
) -> list[float]:
    """Greedy angular clustering: return representative branch angles.

    Each input angle is assigned to the nearest existing cluster center
    if within ``threshold_deg``; otherwise a new center is created.
    Centers are updated via circular mean on assignment.

    Input angles are sorted to produce deterministic output regardless
    of the caller's ordering.
    """
    sorted_angles = sorted(angles)
    centers: list[float] = []
    for angle in sorted_angles:
        nearest: int | None = None
        nearest_delta = 999.0
        for i, center in enumerate(centers):
            delta = abs(_angle_delta_deg(center, angle))
            if delta < nearest_delta:
                nearest = i
                nearest_delta = delta
        if nearest is None or nearest_delta > threshold_deg:
            centers.append(angle)
        else:
            centers[nearest] = _circular_mean_deg([centers[nearest], angle])
    return sorted(centers)


def _nearest_angle_id(angle: float, centers: list[float]) -> int:
    """Return index of the nearest branch center to ``angle``."""
    return min(
        range(len(centers)),
        key=lambda i: abs(_angle_delta_deg(centers[i], angle)),
    )


def _make_observation(
    track: list[TrajectoryPoint],
    points: list[tuple[float, float]],
    *,
    cluster_calculate_index: int,
    center: tuple[float, float],
    entry_seg: int,
    entry_xy: tuple[float, float],
    exit_seg: int,
    exit_xy: tuple[float, float],
    bearing_window: int,
    entry_crossing_calculate_index: int | None = None,
    exit_crossing_calculate_index: int | None = None,
) -> EntryExitObservation | None:
    """Build a single EntryExitObservation from matched entry/exit segments."""
    inbound_angles = _segment_bearings(points, entry_seg - bearing_window, entry_seg + 1)
    outbound_angles = _segment_bearings(points, exit_seg, exit_seg + bearing_window + 1)
    if not inbound_angles or not outbound_angles:
        return None

    inbound_bearing = _circular_mean_deg(inbound_angles)
    outbound_bearing = _circular_mean_deg(outbound_angles)
    return EntryExitObservation(
        cluster_calculate_index=cluster_calculate_index,
        vehicle_id=track[0].vehicle_id,
        entry_branch_angle=_branch_angle_from_center(center, entry_xy),
        exit_branch_angle=_branch_angle_from_center(center, exit_xy),
        entry_bearing=inbound_bearing,
        exit_bearing=outbound_bearing,
        turn_deg=_angle_delta_deg(inbound_bearing, outbound_bearing),
        entry_crossing_calculate_index=entry_crossing_calculate_index,
        exit_crossing_calculate_index=exit_crossing_calculate_index,
    )


def _build_topology_for_cluster(
    cluster: HotspotCluster,
    observations: list[EntryExitObservation],
    cfg: IntersectConfig,
) -> tuple[IntersectionTopology, HotspotCluster, list[EntryExitObservation]]:
    """Build topology for a single cluster from pre-computed observations.

    ZH: 对单个 cluster 构建拓扑：聚类分支角度、构建有向转移矩阵、评估完整性。
        与 ``_build_intersection_topologies`` 不同，此函数接收已计算好的 observations，
        适用于 DB 模式（observations 从 DB 查询的 nearby tracks 中提取）。
    """
    # Branch angle clustering
    branch_angles: list[float] = []
    for obs in observations:
        branch_angles.append(obs.entry_branch_angle)
        branch_angles.append(obs.exit_branch_angle)
    branch_centers = (
        _cluster_angles_greedy(branch_angles, cfg.branch_threshold_deg)
        if branch_angles
        else []
    )

    # Directed transition matrix
    transition_counts: dict[tuple[int, int], int] = {}
    for obs in observations:
        if not branch_centers:
            break
        entry_id = _nearest_angle_id(obs.entry_branch_angle, branch_centers)
        exit_id = _nearest_angle_id(obs.exit_branch_angle, branch_centers)
        key = (entry_id, exit_id)
        transition_counts[key] = transition_counts.get(key, 0) + 1

    is_complete = (
        len(branch_centers) >= cfg.min_branches
        and len(observations) >= cfg.min_observations
        and len(transition_counts) >= cfg.min_directed_transitions
    )
    reason = (
        "complete"
        if is_complete
        else (
            f"branches={len(branch_centers)}, "
            f"observations={len(observations)}, "
            f"directed_transitions={len(transition_counts)}"
        )
    )

    topology = IntersectionTopology(
        cluster_calculate_index=cluster.cluster_calculate_index,
        branch_centers_deg=branch_centers,
        transition_counts=transition_counts,
        observation_count=len(observations),
        is_complete=is_complete,
        reason=reason,
        clip_radius_m=max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        ),
    )
    return topology, cluster, observations


def _build_intersection_topologies(
    clusters: list[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
    transformer: CoordinateTransformer,
    cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[IntersectionTopology], list[HotspotCluster], list[EntryExitObservation]]:
    """Build IntersectionTopology for each HotspotCluster.

    For each cluster:
      1. Batch-compute entry/exit observations for all vehicle tracks
      2. Cluster branch angles (delegates to ``_build_topology_for_cluster``)
      3. Build directed transition matrix
      4. Evaluate completeness
    """
    topologies: list[IntersectionTopology] = []
    annotated_clusters: list[HotspotCluster] = []
    all_observations: list[EntryExitObservation] = []
    total_clusters = len(clusters)
    t0 = time.perf_counter()
    log_info(f"── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")

    for cluster in _perf_iter(perf, clusters, name="topo_clusters", total=total_clusters):
        cluster_idx = len(topologies) + 1
        report_detect_progress(
            live_status,
            detail=f"簇/Cluster:\t{cluster_idx:,}/{total_clusters:,}",
            progress=cluster_idx / total_clusters,
            elapsed=time.perf_counter() - t0,
            stage="Detect: 拓扑验证 / Topology",
        )
        radius = max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        )
        observations, crossing_points = _extract_entry_exit_observations_batch(
            tracks,
            cluster=cluster,
            transformer=transformer,
            radius_m=radius,
            bearing_window=cfg.bearing_window,
        )
        # Attach crossing points to cluster for replay provenance
        cluster = replace(cluster, crossing_points=tuple(crossing_points))

        topology, annotated, cluster_obs = _build_topology_for_cluster(
            cluster, observations, cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_obs)

    log_info(f"── 拓扑验证: {len(topologies):,} 拓扑/topologies ({time.perf_counter() - t0:.1f}s)")
    return topologies, annotated_clusters, all_observations


def _extract_entry_exit_observations_batch(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cluster: HotspotCluster,
    transformer: CoordinateTransformer,
    radius_m: float,
    bearing_window: int = 2,
) -> tuple[list[EntryExitObservation], list[CrossingPoint]]:
    """Compute every state-changing circle root and pair complete entry/exits.

    A segment may contribute two roots (outside → inside → outside). Roots at
    the same trajectory progress are handled as one batch so a vertex touch
    does not create an entry/exit pair with zero road length.
    """
    center = (cluster.centroid_x, cluster.centroid_y)
    starts: list[tuple[float, float]] = []
    ends: list[tuple[float, float]] = []
    segment_meta: list[tuple[str, int]] = []  # (track key, segment index)
    track_points_lookup: dict[str, list[TrajectoryPoint]] = {}
    xy_points_lookup: dict[str, list[tuple[float, float]]] = {}

    for track_key, track in sorted(tracks.items()):
        if len(track) < 2:
            continue
        xy_points = [_point_xy(p) for p in track]
        track_points_lookup[track_key] = track
        xy_points_lookup[track_key] = xy_points
        for seg_idx in range(len(xy_points) - 1):
            starts.append(xy_points[seg_idx])
            ends.append(xy_points[seg_idx + 1])
            segment_meta.append((track_key, seg_idx))

    if not starts:
        return [], []

    starts_xy = np.asarray(starts, dtype=float)
    ends_xy = np.asarray(ends, dtype=float)
    root_counts, root_fractions, crossing_xy_np = _segment_circle_crossings_batch(
        starts_xy, ends_xy, center, radius_m
    )

    # progress, segment, fraction, is_entry, crossing xy
    roots_by_track: dict[
        str,
        list[tuple[float, int, float, bool, tuple[float, float]]],
    ] = defaultdict(list)
    center_x, center_y = center
    for flat_index, (track_key, segment_index) in enumerate(segment_meta):
        start_x, start_y = starts[flat_index]
        delta_x = ends[flat_index][0] - start_x
        delta_y = ends[flat_index][1] - start_y
        for root_index in range(int(root_counts[flat_index])):
            fraction = float(root_fractions[flat_index, root_index])
            crossing_x = float(crossing_xy_np[flat_index, root_index, 0])
            crossing_y = float(crossing_xy_np[flat_index, root_index, 1])
            radial_velocity = (
                (crossing_x - center_x) * delta_x
                + (crossing_y - center_y) * delta_y
            )
            if abs(radial_velocity) <= 1e-9:
                continue
            roots_by_track[track_key].append((
                segment_index + fraction,
                segment_index,
                fraction,
                radial_velocity < 0.0,
                (crossing_x, crossing_y),
            ))

    crossing_counter = 0
    crossing_points: list[CrossingPoint] = []
    observations: list[EntryExitObservation] = []
    for track_key, track in sorted(track_points_lookup.items()):
        roots = sorted(
            roots_by_track.get(track_key, ()),
            key=lambda item: (item[0], 0 if not item[3] else 1, item[1]),
        )
        first_x, first_y = xy_points_lookup[track_key][0]
        inside = math.hypot(first_x - center_x, first_y - center_y) < radius_m
        pending_entry: tuple[int, tuple[float, float], int] | None = None
        cursor = 0
        while cursor < len(roots):
            batch_end = cursor + 1
            while (
                batch_end < len(roots)
                and abs(roots[batch_end][0] - roots[cursor][0]) <= 1e-9
            ):
                batch_end += 1
            batch = roots[cursor:batch_end]
            kinds = {root[3] for root in batch}
            cursor = batch_end

            # An entry and exit at the same progress leaves membership
            # unchanged. This covers shared-vertex touches deterministically.
            if len(kinds) != 1:
                continue
            is_entry = kinds.pop()
            if is_entry == inside:
                # Duplicate root at a shared vertex; one transition was enough.
                continue

            # Prefer the t=0 representation at a shared vertex. It identifies
            # the following real segment and avoids depending on the prior one.
            root = min(batch, key=lambda item: (item[2] != 0.0, item[1]))
            _progress, segment_index, fraction, _kind, crossing_xy = root
            segment_start = track[segment_index]
            refined_ref = segment_start.refined_point_ref
            subtrack_index = (
                refined_ref.subtrack_index if refined_ref is not None else 1
            )
            crossing_lon, crossing_lat = transformer.inverse(*crossing_xy)
            crossing = CrossingPoint(
                crossing_calculate_index=crossing_counter,
                cluster_ref=cluster.cluster_ref,
                vehicle_id=track[0].vehicle_id,
                segment_start=refined_ref,
                subtrack_index=subtrack_index,
                point_ref=segment_start.point_ref,
                crossing_x=crossing_xy[0],
                crossing_y=crossing_xy[1],
                crossing_lon=crossing_lon,
                crossing_lat=crossing_lat,
                is_entry=is_entry,
                branch_angle_deg=_branch_angle_from_center(center, crossing_xy),
                segment_fraction=fraction,
            )
            crossing_points.append(crossing)
            crossing_counter += 1

            inside = is_entry
            if is_entry:
                pending_entry = (
                    segment_index,
                    crossing_xy,
                    crossing.crossing_calculate_index,
                )
                continue
            if pending_entry is None:
                # The subtrack began inside this circle. The boundary exit is
                # still persisted for Clip, but it has no complete observation.
                continue

            entry_segment, entry_xy, entry_index = pending_entry
            pending_entry = None
            observation = _make_observation(
                track,
                xy_points_lookup[track_key],
                cluster_calculate_index=cluster.cluster_calculate_index,
                center=center,
                entry_seg=entry_segment,
                entry_xy=entry_xy,
                exit_seg=segment_index,
                exit_xy=crossing_xy,
                bearing_window=bearing_window,
                entry_crossing_calculate_index=entry_index,
                exit_crossing_calculate_index=crossing.crossing_calculate_index,
            )
            if observation is not None:
                observations.append(observation)
    return observations, crossing_points
