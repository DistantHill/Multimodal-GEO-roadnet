"""Intersect2 — Dual-mode intersection candidate detection and topology extraction.

detect_intersections(source, *, config=None) → IntersectResult

Two complementary methods feed into the same topology verification:
  Method 1 (turn events):   Bearing-change detection → Getis-Ord G* → DBSCAN
  Method 2 (orthogonal):    Orthogonal injection points from Cleaner 1 → DBSCAN

Both produce HotspotCluster candidates that are merged, deduplicated, and
verified via entry/exit circle-crossing analysis + branch-angle clustering.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

from ._geometry import (
    _angle_delta_deg,
    _bearing_deg,
    _branch_angle_from_center,
    _circular_mean_deg,
    _distance_m,
    _segment_bearings,
    _segment_circle_crossing,
    _segment_circle_crossings_batch,
    _to_lonlat,
    _to_xy,
)
from .exceptions import V2Error
from .models import (
    EntryExitObservation,
    HotspotCluster,
    IntersectReport,
    IntersectionTopology,
    OrthogonalCandidate,
    PointRef,
    TrackModel,
    TrajectoryPoint,
    TurnEvent,
)
from .parser0 import TrajectorySourceHandle


# ── Config ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntersectConfig(TrackModel):
    """Intersect2 configuration — dual-mode intersection detection.

    PRD v2 Intersect2 §7
    """

    # ── Mode toggles ───────────────────────────────────────────────
    use_turn_events: bool = True
    use_orthogonal_candidates: bool = True

    # ── Turn event detection (Method 1) ────────────────────────────
    turn_window: int = 2
    min_turn_deg: float = 45.0
    max_turn_deg: float = 170.0
    min_leg_m: float = 35.0
    min_movement_speed_mps: float = 1.5

    # ── Getis-Ord G* hotspot scoring ───────────────────────────────
    g_star_radius_m: float = 180.0
    g_star_min_z: float = 0.5

    # ── DBSCAN (per method) ────────────────────────────────────────
    dbscan_eps_m_turn: float = 140.0
    dbscan_min_samples_turn: int = 4
    dbscan_eps_m_ortho: float = 100.0
    dbscan_min_samples_ortho: int = 3

    # ── Cluster merge ──────────────────────────────────────────────
    merge_centroid_distance_m: float = 80.0

    # ── Topology verification (shared) ─────────────────────────────
    min_intersection_radius_m: float = 90.0
    topology_radius_padding_m: float = 70.0
    branch_threshold_deg: float = 35.0
    min_branches: int = 3
    min_observations: int = 6
    min_directed_transitions: int = 3
    bearing_window: int = 2


# ── Result ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntersectResult(TrackModel):
    """Output of Intersect2 stage.

    All fields populated eagerly during detect_intersections().
    """

    turn_events: list[TurnEvent]
    hotspot_events: list[TurnEvent]
    orthogonal_candidates: list[OrthogonalCandidate]
    clusters: list[HotspotCluster]  # merged from both methods
    topologies: list[IntersectionTopology]
    report: IntersectReport

    def get_report(self) -> IntersectReport:
        return self.report


# ── Public entry point ─────────────────────────────────────────────────


def detect_intersections(
    source: TrajectorySourceHandle,
    *,
    config: IntersectConfig | None = None,
) -> IntersectResult:
    """Run dual-mode intersection detection and topology extraction.

    PRD v2 Intersect2 §2
    """
    cfg = config or IntersectConfig()

    # ── Buffer all points, group by vehicle ────────────────────────
    tracks, origin_lon, origin_lat = _group_by_vehicle(source)
    if not tracks:
        return IntersectResult(
            turn_events=[],
            hotspot_events=[],
            orthogonal_candidates=[],
            clusters=[],
            topologies=[],
            report=IntersectReport(
                track_count=0,
                turn_event_count=0,
                hotspot_event_count=0,
                orthogonal_candidate_count=0,
                cluster_count=0,
                cluster_count_turn=0,
                cluster_count_ortho=0,
                complete_intersection_count=0,
            ),
        )

    turn_events: list[TurnEvent] = []
    hotspot_events: list[TurnEvent] = []
    orthogonal_candidates: list[OrthogonalCandidate] = []
    all_clusters: list[HotspotCluster] = []

    # ── Method 1: Turn event → Getis-Ord G* → DBSCAN ──────────────
    if cfg.use_turn_events:
        turn_events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg)
        if turn_events:
            hotspot_events = _select_hotspot_events(turn_events, cfg)
            if hotspot_events:
                clusters_turn = _cluster_dbscan(
                    hotspot_events,
                    origin_lon,
                    origin_lat,
                    eps_m=cfg.dbscan_eps_m_turn,
                    min_samples=cfg.dbscan_min_samples_turn,
                    source="turn",
                )
                all_clusters.extend(clusters_turn)

    # ── Method 2: Orthogonal injection points → DBSCAN ────────────
    if cfg.use_orthogonal_candidates:
        orthogonal_candidates = _extract_orthogonal_candidates(
            tracks, origin_lon, origin_lat
        )
        if orthogonal_candidates:
            clusters_ortho = _cluster_orthogonal_candidates(
                orthogonal_candidates,
                origin_lon,
                origin_lat,
                eps_m=cfg.dbscan_eps_m_ortho,
                min_samples=cfg.dbscan_min_samples_ortho,
            )
            all_clusters.extend(clusters_ortho)

    # ── Merge overlapping clusters from both methods ──────────────
    merged_clusters = _merge_clusters(all_clusters, cfg.merge_centroid_distance_m,
                                         origin_lon, origin_lat)

    # ── Reassign globally-unique cluster IDs (Methods 1 & 2 start from 0) ─
    merged_clusters = [
        HotspotCluster(
            cluster_id=new_id,
            centroid_x=c.centroid_x,
            centroid_y=c.centroid_y,
            centroid_lon=c.centroid_lon,
            centroid_lat=c.centroid_lat,
            radius_m=c.radius_m,
            events=c.events,
            source=c.source,
        )
        for new_id, c in enumerate(merged_clusters)
    ]

    # ── Topology verification for each cluster ─────────────────────
    topologies = _build_intersection_topologies(
        merged_clusters, tracks, origin_lon, origin_lat, cfg
    )

    # ── Build report ───────────────────────────────────────────────
    turn_clusters = sum(1 for c in merged_clusters if c.source == "turn")
    ortho_clusters = sum(1 for c in merged_clusters if c.source == "orthogonal")

    report = IntersectReport(
        track_count=len(tracks),
        turn_event_count=len(turn_events),
        hotspot_event_count=len(hotspot_events),
        orthogonal_candidate_count=len(orthogonal_candidates),
        cluster_count=len(merged_clusters),
        cluster_count_turn=turn_clusters,
        cluster_count_ortho=ortho_clusters,
        complete_intersection_count=sum(1 for t in topologies if t.is_complete),
    )

    return IntersectResult(
        turn_events=turn_events,
        hotspot_events=hotspot_events,
        orthogonal_candidates=orthogonal_candidates,
        clusters=merged_clusters,
        topologies=topologies,
        report=report,
    )


# ── Step 1: Group by vehicle ──────────────────────────────────────────


def _group_by_vehicle(
    handle: TrajectorySourceHandle,
) -> tuple[dict[str, list[TrajectoryPoint]], float, float]:
    """Consume full stream, group points by vehicle_id.

    Returns (tracks, origin_lon, origin_lat). Origin is set from the
    first point encountered in the stream.
    """
    tracks: dict[str, list[TrajectoryPoint]] = {}
    origin_lon: float | None = None
    origin_lat: float | None = None

    for tp in handle.iter_points():
        if origin_lon is None:
            origin_lon = tp.lon
            origin_lat = tp.lat
        if tp.vehicle_id not in tracks:
            tracks[tp.vehicle_id] = []
        tracks[tp.vehicle_id].append(tp)

    if origin_lon is None:
        raise V2Error("No points in source — cannot detect intersections")

    return tracks, origin_lon, origin_lat


# ═══════════════════════════════════════════════════════════════════════
# Method 1: Turn event detection + Getis-Ord G* hotspot scoring
# ═══════════════════════════════════════════════════════════════════════


def _detect_turn_events(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
) -> list[TurnEvent]:
    """Multi-point window bearing-change detection for all tracks.

    For each track, slides a window of ``turn_window`` points before and
    after each candidate index. Computes in/out bearings via circular mean
    of segment bearings, then detects a turn if the absolute angle delta
    falls within ``[min_turn_deg, max_turn_deg]``.
    """
    events: list[TurnEvent] = []
    w = cfg.turn_window

    for vehicle_id, track in tracks.items():
        if len(track) < 2 * w + 1:
            continue

        xy = [_to_xy(p.lon, p.lat, origin_lon, origin_lat) for p in track]

        for i in range(w, len(track) - w):
            in_leg = _distance_m(xy[i - w], xy[i])
            out_leg = _distance_m(xy[i], xy[i + w])
            elapsed = track[i + w].timestamp - track[i - w].timestamp
            if elapsed <= 0.0:
                continue  # degenerate or duplicate timestamps — skip
            movement_speed = (in_leg + out_leg) / elapsed

            if (
                in_leg < cfg.min_leg_m
                or out_leg < cfg.min_leg_m
                or movement_speed < cfg.min_movement_speed_mps
            ):
                continue

            in_angles = _segment_bearings(xy, i - w, i)
            out_angles = _segment_bearings(xy, i, i + w)
            if not in_angles or not out_angles:
                continue

            in_bearing = _circular_mean_deg(in_angles)
            out_bearing = _circular_mean_deg(out_angles)
            turn = _angle_delta_deg(in_bearing, out_bearing)
            abs_turn = abs(turn)

            if cfg.min_turn_deg <= abs_turn <= cfg.max_turn_deg:
                p = track[i]
                events.append(
                    TurnEvent(
                        event_id=len(events),
                        vehicle_id=vehicle_id,
                        timestamp=p.timestamp,
                        lon=p.lon,
                        lat=p.lat,
                        x=xy[i][0],
                        y=xy[i][1],
                        in_bearing=in_bearing,
                        out_bearing=out_bearing,
                        turn_deg=turn,
                        score=abs_turn,
                    )
                )

    return events


def _compute_getis_ord_g_star(
    events: list[TurnEvent], radius_m: float
) -> dict[int, float]:
    """Return {event_id: z_score} for Getis-Ord G* spatial concentration.

    Uses a binary intensity model: each event contributes 1.0 to local
    count if within ``radius_m``. Since TurnEvent is frozen, z-scores
    are returned as a separate dict instead of mutating event fields.
    """
    n = len(events)
    if n < 2:
        return {e.event_id: 0.0 for e in events}

    local_counts: list[float] = []
    for event in events:
        count = 0.0
        ex, ey = event.x, event.y
        for other in events:
            dx = other.x - ex
            dy = other.y - ey
            if dx * dx + dy * dy <= radius_m * radius_m:
                count += 1.0
        local_counts.append(count)

    mean = sum(local_counts) / n
    variance = sum(v * v for v in local_counts) / n - mean * mean
    std = math.sqrt(max(variance, 0.0))

    if std == 0.0:
        return {e.event_id: 0.0 for e in events}

    return {
        e.event_id: (lc - mean) / std
        for e, lc in zip(events, local_counts)
    }


def _select_hotspot_events(
    events: list[TurnEvent], cfg: IntersectConfig
) -> list[TurnEvent]:
    """Filter turn events by Getis-Ord G* z-score and minimum turn angle."""
    if not events:
        return []

    z_scores = _compute_getis_ord_g_star(events, cfg.g_star_radius_m)
    return [
        e
        for e in events
        if z_scores.get(e.event_id, 0.0) >= cfg.g_star_min_z
        and abs(e.turn_deg) >= cfg.min_turn_deg
    ]


# ═══════════════════════════════════════════════════════════════════════
# Method 2: Orthogonal intersection candidate extraction
# ═══════════════════════════════════════════════════════════════════════


def _extract_orthogonal_candidates(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
) -> list[OrthogonalCandidate]:
    """Extract Cleaner 1 orthogonal injection points as candidates.

    These are synthetic TrajectoryPoints with
    ``point_ref.generated_reason == "orthogonal_heading"``. They mark
    locations where two consecutive GPS headings are ~90° apart,
    suggesting a road intersection.
    """
    candidates: list[OrthogonalCandidate] = []

    for track in tracks.values():
        for tp in track:
            if tp.point_ref.generated_reason == "orthogonal_heading":
                x, y = _to_xy(tp.lon, tp.lat, origin_lon, origin_lat)
                candidates.append(
                    OrthogonalCandidate(
                        point_ref=tp.point_ref,
                        vehicle_id=tp.vehicle_id,
                        timestamp=tp.timestamp,
                        lon=tp.lon,
                        lat=tp.lat,
                        x=x,
                        y=y,
                    )
                )

    return candidates


# ═══════════════════════════════════════════════════════════════════════
# DBSCAN clustering (shared by both methods with different parameters)
# ═══════════════════════════════════════════════════════════════════════

_UNVISITED = -99
_NOISE = -1


def _neighbors_indices(
    xs: list[float],
    ys: list[float],
    index: int,
    eps_m: float,
) -> list[int]:
    """Return indices of points within eps_m of the point at ``index``."""
    base_x, base_y = xs[index], ys[index]
    result: list[int] = []
    eps_sq = eps_m * eps_m
    for i, (xx, yy) in enumerate(zip(xs, ys)):
        dx = xx - base_x
        dy = yy - base_y
        if dx * dx + dy * dy <= eps_sq:
            result.append(i)
    return result


def _run_dbscan(
    xs: list[float],
    ys: list[float],
    *,
    eps_m: float,
    min_samples: int,
) -> list[int]:
    """Pure Python DBSCAN on (xs, ys) coordinates.

    Returns a labels list of length n.  Cluster IDs are 0-based;
    noise points have label ``_NOISE`` (-1).
    """
    n = len(xs)
    if n == 0:
        return []

    labels = [_UNVISITED] * n
    cluster_id = 0

    for i in range(n):
        if labels[i] != _UNVISITED:
            continue
        neighbors = _neighbors_indices(xs, ys, i, eps_m)
        if len(neighbors) < min_samples:
            labels[i] = _NOISE
            continue

        labels[i] = cluster_id
        seeds = [nb for nb in neighbors if nb != i]
        while seeds:
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_id
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_id
            cur_neighbors = _neighbors_indices(xs, ys, current, eps_m)
            if len(cur_neighbors) >= min_samples:
                for nb in cur_neighbors:
                    if labels[nb] in (_UNVISITED, _NOISE) and nb not in seeds:
                        seeds.append(nb)
        cluster_id += 1

    return labels


def _cluster_dbscan(
    events: list[TurnEvent],
    origin_lon: float,
    origin_lat: float,
    *,
    eps_m: float,
    min_samples: int,
    source: str = "turn",
) -> list[HotspotCluster]:
    """DBSCAN-cluster TurnEvents and build HotspotClusters."""
    n = len(events)
    if n == 0:
        return []

    xs = [e.x for e in events]
    ys = [e.y for e in events]
    labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples)

    clusters: list[HotspotCluster] = []
    for cid in sorted(set(l for l in labels if l >= 0)):
        cluster_items = [events[i] for i, l in enumerate(labels) if l == cid]
        if not cluster_items:
            continue
        cx = sum(e.x for e in cluster_items) / len(cluster_items)
        cy = sum(e.y for e in cluster_items) / len(cluster_items)
        radius = max(_distance_m((cx, cy), (e.x, e.y)) for e in cluster_items)
        lon, lat = _to_lonlat(cx, cy, origin_lon, origin_lat)
        clusters.append(
            HotspotCluster(
                cluster_id=cid,
                centroid_x=cx,
                centroid_y=cy,
                centroid_lon=lon,
                centroid_lat=lat,
                radius_m=radius,
                events=tuple(cluster_items),
                source=source,
            )
        )
    return clusters


def _cluster_orthogonal_candidates(
    candidates: list[OrthogonalCandidate],
    origin_lon: float,
    origin_lat: float,
    *,
    eps_m: float,
    min_samples: int,
) -> list[HotspotCluster]:
    """DBSCAN-cluster OrthogonalCandidates, wrapping them as TurnEvents."""
    n = len(candidates)
    if n == 0:
        return []

    xs = [c.x for c in candidates]
    ys = [c.y for c in candidates]
    labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples)

    clusters: list[HotspotCluster] = []
    for cid in sorted(set(l for l in labels if l >= 0)):
        cluster_items = [candidates[i] for i, l in enumerate(labels) if l == cid]
        if not cluster_items:
            continue
        cx = sum(c.x for c in cluster_items) / len(cluster_items)
        cy = sum(c.y for c in cluster_items) / len(cluster_items)
        radius = max(_distance_m((cx, cy), (c.x, c.y)) for c in cluster_items)
        lon, lat = _to_lonlat(cx, cy, origin_lon, origin_lat)
        cluster_events = tuple(
            TurnEvent(
                event_id=i,
                vehicle_id=c.vehicle_id,
                timestamp=c.timestamp,
                lon=c.lon,
                lat=c.lat,
                x=c.x,
                y=c.y,
                in_bearing=0.0,
                out_bearing=0.0,
                turn_deg=0.0,
                score=0.0,
            )
            for i, c in enumerate(cluster_items)
        )
        clusters.append(
            HotspotCluster(
                cluster_id=cid,
                centroid_x=cx,
                centroid_y=cy,
                centroid_lon=lon,
                centroid_lat=lat,
                radius_m=radius,
                events=cluster_events,
                source="orthogonal",
            )
        )
    return clusters


# ═══════════════════════════════════════════════════════════════════════
# Cluster merge (deduplicate overlapping clusters from both methods)
# ═══════════════════════════════════════════════════════════════════════


def _merge_clusters(
    clusters: list[HotspotCluster],
    merge_distance_m: float,
    origin_lon: float,
    origin_lat: float,
) -> list[HotspotCluster]:
    """Merge clusters whose centroids are within ``merge_distance_m``.

    When two clusters overlap, keep the one with more events. The surviving
    cluster's source field is set to "merged" if it absorbed another method's
    cluster.

    Uses an iterated greedy merge: after each absorption the merged centroid
    is recomputed and remaining clusters are re-checked against it, preventing
    the non-transitive merge problem.
    """
    if len(clusters) <= 1:
        return list(clusters)

    # Sort by event count descending — larger clusters absorb smaller ones
    remaining = sorted(
        clusters, key=lambda c: len(c.events), reverse=True
    )
    kept: list[HotspotCluster] = []

    while remaining:
        ci = remaining.pop(0)
        merged_source = ci.source
        merged_events = list(ci.events)
        cx_sum = ci.centroid_x * len(ci.events)
        cy_sum = ci.centroid_y * len(ci.events)
        total_n = len(ci.events)
        # Track max distance from current merged centroid for radius
        max_sq = ci.radius_m * ci.radius_m if total_n > 0 else 0.0

        # Repeatedly scan for clusters that are within merge_distance_m
        # of the CURRENT merged centroid (updated after each absorption)
        changed = True
        while changed:
            changed = False
            survivors: list[HotspotCluster] = []
            # Current merged centroid
            cur_cx = cx_sum / total_n if total_n > 0 else ci.centroid_x
            cur_cy = cy_sum / total_n if total_n > 0 else ci.centroid_y

            for cj in remaining:
                dist = _distance_m((cur_cx, cur_cy), (cj.centroid_x, cj.centroid_y))
                if dist <= merge_distance_m:
                    # Absorb cj
                    if cj.source != merged_source:
                        merged_source = "merged"
                    merged_events.extend(cj.events)
                    # Update radius: new_radius = max(old_radius, dist + cj.radius_m)
                    candidate_r = dist + cj.radius_m
                    if candidate_r * candidate_r > max_sq:
                        max_sq = candidate_r * candidate_r
                    cx_sum += cj.centroid_x * len(cj.events)
                    cy_sum += cj.centroid_y * len(cj.events)
                    total_n += len(cj.events)
                    changed = True
                else:
                    survivors.append(cj)
            remaining = survivors

        if total_n > 0:
            new_cx = cx_sum / total_n
            new_cy = cy_sum / total_n
            merged_radius = math.sqrt(max(max_sq, 0.0))
        else:
            new_cx = ci.centroid_x
            new_cy = ci.centroid_y
            merged_radius = ci.radius_m

        new_lon, new_lat = _to_lonlat(new_cx, new_cy, origin_lon, origin_lat)
        kept.append(
            HotspotCluster(
                cluster_id=ci.cluster_id,
                centroid_x=new_cx,
                centroid_y=new_cy,
                centroid_lon=new_lon,
                centroid_lat=new_lat,
                radius_m=merged_radius,
                events=tuple(merged_events),
                source=merged_source,
            )
        )

    return kept


# ═══════════════════════════════════════════════════════════════════════
# Topology verification (entry/exit + branch clustering + completeness)
# ═══════════════════════════════════════════════════════════════════════


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
    xy: list[tuple[float, float]],
    *,
    cluster_id: int,
    center: tuple[float, float],
    entry_seg: int,
    entry_xy: tuple[float, float],
    exit_seg: int,
    exit_xy: tuple[float, float],
    bearing_window: int,
) -> EntryExitObservation | None:
    """Build a single EntryExitObservation from matched entry/exit segments."""
    inbound_angles = _segment_bearings(xy, entry_seg - bearing_window, entry_seg + 1)
    outbound_angles = _segment_bearings(xy, exit_seg, exit_seg + bearing_window + 1)
    if not inbound_angles or not outbound_angles:
        return None

    inbound_bearing = _circular_mean_deg(inbound_angles)
    outbound_bearing = _circular_mean_deg(outbound_angles)
    return EntryExitObservation(
        cluster_id=cluster_id,
        vehicle_id=track[0].vehicle_id,
        entry_branch_angle=_branch_angle_from_center(center, entry_xy),
        exit_branch_angle=_branch_angle_from_center(center, exit_xy),
        inbound_bearing=inbound_bearing,
        outbound_bearing=outbound_bearing,
        turn_deg=_angle_delta_deg(inbound_bearing, outbound_bearing),
    )


def _build_intersection_topologies(
    clusters: list[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
) -> list[IntersectionTopology]:
    """Build IntersectionTopology for each HotspotCluster.

    For each cluster:
      1. Batch-compute entry/exit observations for all vehicle tracks
      2. Cluster branch angles
      3. Build directed transition matrix
      4. Evaluate completeness
    """
    topologies: list[IntersectionTopology] = []

    for cluster in clusters:
        radius = max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        )
        observations = _extract_entry_exit_observations_batch(
            tracks,
            cluster=cluster,
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            radius_m=radius,
            bearing_window=cfg.bearing_window,
        )

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

        topologies.append(
            IntersectionTopology(
                cluster_id=cluster.cluster_id,
                branch_centers_deg=branch_centers,
                transition_counts=transition_counts,
                observation_count=len(observations),
                is_complete=is_complete,
                reason=reason,
            )
        )

    return topologies


def _extract_entry_exit_observations_batch(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cluster: HotspotCluster,
    origin_lon: float,
    origin_lat: float,
    radius_m: float,
    bearing_window: int = 2,
) -> list[EntryExitObservation]:
    """Batch-compute entry/exit observations for one intersection candidate.

    All trajectory segments are flattened into ``(n, 2)`` matrices and
    intersected with the candidate circle in one NumPy call. The Python
    loop that remains only walks segment metadata to pair each vehicle's
    first entry with the following exit.
    """
    center = (cluster.centroid_x, cluster.centroid_y)
    starts: list[tuple[float, float]] = []
    ends: list[tuple[float, float]] = []
    segment_meta: list[tuple[str, int]] = []  # (vehicle_id, seg_idx)
    track_lookup: dict[str, list[TrajectoryPoint]] = {}
    xy_lookup: dict[str, list[tuple[float, float]]] = {}

    for vehicle_id, track in tracks.items():
        if len(track) < 2:
            continue
        xy = [_to_xy(p.lon, p.lat, origin_lon, origin_lat) for p in track]
        track_lookup[vehicle_id] = track
        xy_lookup[vehicle_id] = xy
        for seg_idx in range(len(xy) - 1):
            starts.append(xy[seg_idx])
            ends.append(xy[seg_idx + 1])
            segment_meta.append((vehicle_id, seg_idx))

    if not starts:
        return []

    start_xy = np.asarray(starts, dtype=float)
    end_xy = np.asarray(ends, dtype=float)
    center_xy = np.asarray(center, dtype=float)
    radius_sq = radius_m * radius_m
    start_inside = (
        np.einsum("ij,ij->i", start_xy - center_xy, start_xy - center_xy)
        <= radius_sq
    )
    end_inside = (
        np.einsum("ij,ij->i", end_xy - center_xy, end_xy - center_xy)
        <= radius_sq
    )
    hit_mask, crossing_xy_np = _segment_circle_crossings_batch(
        start_xy, end_xy, center, radius_m
    )

    entry_mask = (~start_inside) & end_inside & hit_mask
    exit_mask = start_inside & (~end_inside) & hit_mask
    entry_by_vehicle: dict[str, tuple[int, tuple[float, float]]] = {}
    exit_by_vehicle: dict[str, tuple[int, tuple[float, float]]] = {}

    for flat_idx, (vehicle_id, seg_idx) in enumerate(segment_meta):
        if vehicle_id not in entry_by_vehicle and entry_mask[flat_idx]:
            entry_by_vehicle[vehicle_id] = (
                seg_idx,
                (float(crossing_xy_np[flat_idx][0]), float(crossing_xy_np[flat_idx][1])),
            )
            continue
        if (
            vehicle_id in entry_by_vehicle
            and vehicle_id not in exit_by_vehicle
            and exit_mask[flat_idx]
        ):
            entry_seg = entry_by_vehicle[vehicle_id][0]
            if seg_idx >= entry_seg:
                exit_by_vehicle[vehicle_id] = (
                    seg_idx,
                    (float(crossing_xy_np[flat_idx][0]), float(crossing_xy_np[flat_idx][1])),
                )

    observations: list[EntryExitObservation] = []
    for vehicle_id, (entry_seg, entry_xy) in entry_by_vehicle.items():
        exit_ = exit_by_vehicle.get(vehicle_id)
        if exit_ is None:
            continue
        exit_seg, exit_xy = exit_
        obs = _make_observation(
            track_lookup[vehicle_id],
            xy_lookup[vehicle_id],
            cluster_id=cluster.cluster_id,
            center=center,
            entry_seg=entry_seg,
            entry_xy=entry_xy,
            exit_seg=exit_seg,
            exit_xy=exit_xy,
            bearing_window=bearing_window,
        )
        if obs is not None:
            observations.append(obs)
    return observations
