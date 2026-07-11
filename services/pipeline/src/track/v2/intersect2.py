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
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
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
    CrossingPoint,
    EntryExitObservation,
    HotspotCluster,
    InOutIntersectionPoint,
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

    # ── Spatial sliding window (DB mode) ───────────────────────────
    query_active_from_db: bool = False
    db_table: str = "trajectory_points"
    spatial_window_size: tuple[float, float] = (2000.0, 2000.0)
    spatial_window_overlap: float = 0.15


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
    observations: list[EntryExitObservation] = field(default_factory=list)

    def get_report(self) -> IntersectReport:
        return self.report


def _ratio_dict(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {source: count / total for source, count in sorted(counts.items())}


def _cluster_sources(cluster: HotspotCluster) -> tuple[str, ...]:
    if cluster.sources:
        return cluster.sources
    if cluster.source_counts:
        return tuple(sorted(source for source, count in cluster.source_counts.items() if count > 0))
    if cluster.source and cluster.source != "merged":
        return (cluster.source,)
    sources = sorted({event.source for event in cluster.events if event.source})
    return tuple(sources)


def _source_label(sources: tuple[str, ...]) -> str:
    if len(sources) == 1:
        return sources[0]
    if len(sources) > 1:
        return "merged"
    return "unknown"


def _cluster_source_counts(cluster: HotspotCluster) -> dict[str, int]:
    if cluster.source_counts:
        return dict(cluster.source_counts)
    sources = _cluster_sources(cluster)
    if len(sources) == 1:
        return {sources[0]: len(cluster.events)}
    counts: dict[str, int] = {}
    for event in cluster.events:
        counts[event.source] = counts.get(event.source, 0) + 1
    return counts


def _cluster_source_point_refs(cluster: HotspotCluster) -> dict[str, tuple[PointRef, ...]]:
    if cluster.source_point_refs:
        return dict(cluster.source_point_refs)
    refs: dict[str, list[PointRef]] = defaultdict(list)
    fallback_sources = _cluster_sources(cluster)
    fallback = fallback_sources[0] if len(fallback_sources) == 1 else cluster.source
    for event in cluster.events:
        source = event.source or fallback
        if event.point_ref is not None:
            refs[source].append(event.point_ref)
    return {source: tuple(items) for source, items in refs.items()}


def _with_source_provenance(
    cluster: HotspotCluster,
    *,
    source_counts: dict[str, int] | None = None,
    source_point_refs: dict[str, tuple[PointRef, ...]] | None = None,
    critical_sources: tuple[str, ...] | None = None,
) -> HotspotCluster:
    counts = source_counts if source_counts is not None else _cluster_source_counts(cluster)
    counts = {source: count for source, count in counts.items() if count > 0}
    sources = tuple(sorted(counts))
    refs = source_point_refs if source_point_refs is not None else _cluster_source_point_refs(cluster)
    return replace(
        cluster,
        source=_source_label(sources),
        sources=sources,
        source_counts=counts,
        source_ratios=_ratio_dict(counts),
        source_point_refs={source: tuple(items) for source, items in refs.items()},
        critical_sources=tuple(sorted(critical_sources or cluster.critical_sources)),
    )


def _merge_count_dicts(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    merged = dict(a)
    for source, count in b.items():
        merged[source] = merged.get(source, 0) + count
    return merged


def _merge_ref_dicts(
    a: dict[str, tuple[PointRef, ...]],
    b: dict[str, tuple[PointRef, ...]],
) -> dict[str, tuple[PointRef, ...]]:
    merged: dict[str, list[PointRef]] = {
        source: list(items)
        for source, items in a.items()
    }
    for source, items in b.items():
        merged.setdefault(source, []).extend(items)
    return {source: tuple(items) for source, items in merged.items()}


def _build_candidate_clusters(
    hotspot_events: list[TurnEvent],
    orthogonal_candidates: list[OrthogonalCandidate],
    cfg: IntersectConfig,
    origin_lon: float,
    origin_lat: float,
    *,
    disabled_sources: set[str] | None = None,
) -> list[HotspotCluster]:
    disabled = disabled_sources or set()
    clusters: list[HotspotCluster] = []
    if "turn" not in disabled and hotspot_events:
        clusters.extend(
            _cluster_dbscan(
                hotspot_events,
                origin_lon,
                origin_lat,
                eps_m=cfg.dbscan_eps_m_turn,
                min_samples=cfg.dbscan_min_samples_turn,
                source="turn",
            )
        )
    if "orthogonal" not in disabled and orthogonal_candidates:
        clusters.extend(
            _cluster_orthogonal_candidates(
                orthogonal_candidates,
                origin_lon,
                origin_lat,
                eps_m=cfg.dbscan_eps_m_ortho,
                min_samples=cfg.dbscan_min_samples_ortho,
            )
        )
    return _merge_clusters(clusters, cfg.merge_centroid_distance_m, origin_lon, origin_lat)


def _is_cluster_rediscovered(
    cluster: HotspotCluster,
    candidates: list[HotspotCluster],
    *,
    distance_m: float,
) -> bool:
    for candidate in candidates:
        if _distance_m(
            (cluster.centroid_x, cluster.centroid_y),
            (candidate.centroid_x, candidate.centroid_y),
        ) <= distance_m:
            return True
    return False


def _annotate_source_ablation(
    clusters: list[HotspotCluster],
    hotspot_events: list[TurnEvent],
    orthogonal_candidates: list[OrthogonalCandidate],
    cfg: IntersectConfig,
    origin_lon: float,
    origin_lat: float,
) -> list[HotspotCluster]:
    if not clusters:
        return []

    all_sources = sorted({source for cluster in clusters for source in _cluster_sources(cluster)})
    report_ablation = len(all_sources) > 1  # only print ablation progress when there are multiple sources
    if report_ablation:
        print(f"    ... 源消融分析 / Source ablation ({len(all_sources)} 源/sources) ...")
        t_abl = time.perf_counter()
    reclustered_without: dict[str, list[HotspotCluster]] = {}
    for source in all_sources:
        if report_ablation:
            print(f"      ... 消融 / Ablating: {source} ...")
            t1 = time.perf_counter()
        reclustered_without[source] = _build_candidate_clusters(
            hotspot_events,
            orthogonal_candidates,
            cfg,
            origin_lon,
            origin_lat,
            disabled_sources={source},
        )
        if report_ablation:
            print(f"      ... 消融 / Ablated {source}: {len(reclustered_without[source])} 簇/clusters ({time.perf_counter() - t1:.1f}s)")

    annotated: list[HotspotCluster] = []
    for cluster in clusters:
        critical_sources: list[str] = []
        for source in _cluster_sources(cluster):
            if not _is_cluster_rediscovered(
                cluster,
                reclustered_without.get(source, []),
                distance_m=cfg.merge_centroid_distance_m,
            ):
                critical_sources.append(source)
        annotated.append(
            _with_source_provenance(
                cluster,
                critical_sources=tuple(critical_sources),
            )
        )
    if report_ablation:
        print(f"    ... 源消融分析: {len(annotated):,} 簇/clusters ({time.perf_counter() - t_abl:.1f}s)")
    return annotated


def _complete_source_report(
    clusters: list[HotspotCluster],
    topologies: list[IntersectionTopology],
) -> dict[str, dict[str, Any]]:
    complete_ids = {topology.cluster_id for topology in topologies if topology.is_complete}
    complete_clusters = [cluster for cluster in clusters if cluster.cluster_id in complete_ids]

    source_counts: dict[str, int] = {}
    critical_source_counts: dict[str, int] = {}
    for cluster in complete_clusters:
        source_counts = _merge_count_dicts(source_counts, _cluster_source_counts(cluster))
        for source in cluster.critical_sources:
            critical_source_counts[source] = critical_source_counts.get(source, 0) + 1

    complete_total = max(1, len(complete_clusters))
    return {
        "source_counts": source_counts,
        "source_ratios": _ratio_dict(source_counts),
        "critical_source_counts": critical_source_counts,
        "critical_source_ratios": {
            source: count / complete_total
            for source, count in sorted(critical_source_counts.items())
        },
    }


# ── Public entry point ─────────────────────────────────────────────────


def detect_intersections(
    source: TrajectorySourceHandle | None = None,
    *,
    config: IntersectConfig | None = None,
    conn: Any = None,
) -> IntersectResult:
    """Run dual-mode intersection detection and topology extraction.

    PRD v2 Intersect2 §2

    Two modes:
      - **Handle mode** (default): ``source`` is a ``TrajectorySourceHandle``.
        All points are read into memory and grouped by vehicle_id.
      - **DB mode**: ``conn`` is a psycopg connection and
        ``config.query_active_from_db`` is True.  Points are queried from
        the database in spatial sliding windows, dramatically reducing
        in-memory footprint for large datasets.
    """
    cfg = config or IntersectConfig()

    # ── DB mode: spatial sliding windows ────────────────────────────
    if conn is not None and cfg.query_active_from_db:
        return _detect_intersections_db(conn, cfg)

    # ── Handle mode: in-memory (original path) ──────────────────────
    if source is None:
        raise V2Error("detect_intersections: 'source' handle is required when not in DB mode")

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
        replace(c, cluster_id=new_id)
        for new_id, c in enumerate(merged_clusters)
    ]

    merged_clusters = _annotate_source_ablation(
        merged_clusters,
        hotspot_events,
        orthogonal_candidates,
        cfg,
        origin_lon,
        origin_lat,
    )

    # ── Topology verification for each cluster ─────────────────────
    topologies, annotated_clusters, all_observations = _build_intersection_topologies(
        merged_clusters, tracks, origin_lon, origin_lat, cfg
    )

    # ── Build report ───────────────────────────────────────────────
    turn_clusters = sum(1 for c in annotated_clusters if "turn" in _cluster_sources(c))
    ortho_clusters = sum(1 for c in annotated_clusters if "orthogonal" in _cluster_sources(c))
    source_report = _complete_source_report(annotated_clusters, topologies)

    report = IntersectReport(
        track_count=len(tracks),
        turn_event_count=len(turn_events),
        hotspot_event_count=len(hotspot_events),
        orthogonal_candidate_count=len(orthogonal_candidates),
        cluster_count=len(annotated_clusters),
        cluster_count_turn=turn_clusters,
        cluster_count_ortho=ortho_clusters,
        complete_intersection_count=sum(1 for t in topologies if t.is_complete),
        complete_intersection_source_counts=source_report["source_counts"],
        complete_intersection_source_ratios=source_report["source_ratios"],
        complete_intersection_critical_source_counts=source_report["critical_source_counts"],
        complete_intersection_critical_source_ratios=source_report["critical_source_ratios"],
    )

    return IntersectResult(
        turn_events=turn_events,
        hotspot_events=hotspot_events,
        orthogonal_candidates=orthogonal_candidates,
        clusters=annotated_clusters,
        topologies=topologies,
        observations=all_observations,
        report=report,
    )


# ═══════════════════════════════════════════════════════════════════════
# Spatial sliding window (DB mode)
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class SpatialWindow:
    """A single spatial cell with optional overlap padding."""
    x_min: float
    y_min: float
    x_max: float
    y_max: float


class SpatialWindowIterator:
    """Divide a spatial extent into overlapping windows.

    ZH: 将 lon/lat 范围划分为重叠的空间滑窗。overlap 是四向扩展比例
        （每侧扩展 overlap%），相邻窗口间实际重叠 = 2 × overlap。

    Parameters
    ----------
    extent : (lon_min, lat_min, lon_max, lat_max)
        Full spatial extent of the data.
    size : (width_m, height_m)
        Window size in meters (approximate, converted to degrees).
    overlap : float
        Per-side expansion ratio (default 0.15 = 15% each side).
    """

    def __init__(
        self,
        extent: tuple[float, float, float, float],
        size: tuple[float, float] = (2000.0, 2000.0),
        overlap: float = 0.15,
    ) -> None:
        self._lon_min, self._lat_min, self._lon_max, self._lat_max = extent
        self._width_m, self._height_m = size
        self._overlap = overlap

        # Approximate metres → degrees (1° ≈ 111,320 m at equator)
        self._step_lon = self._width_m / 111320.0
        self._step_lat = self._height_m / 111320.0
        self._pad_lon = self._step_lon * overlap
        self._pad_lat = self._step_lat * overlap

    def iter_windows(self) -> Iterator[SpatialWindow]:
        """Yield overlapping spatial windows covering the full extent."""
        lon = self._lon_min
        while lon < self._lon_max:
            lat = self._lat_min
            while lat < self._lat_max:
                yield SpatialWindow(
                    x_min=max(self._lon_min, lon - self._pad_lon),
                    y_min=max(self._lat_min, lat - self._pad_lat),
                    x_max=min(self._lon_max, lon + self._step_lon + self._pad_lon),
                    y_max=min(self._lat_max, lat + self._step_lat + self._pad_lat),
                )
                lat += self._step_lat
            lon += self._step_lon


def _query_spatial_extent(conn: Any, *, table: str = "trajectory_points") -> tuple[float, float, float, float] | None:
    """Query MIN/MAX lon/lat of active trajectory points.

    Returns ``(lon_min, lat_min, lon_max, lat_max)`` compatible with
    ``SpatialWindowIterator``.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) "
            f"FROM {table} WHERE is_active IS NOT FALSE"
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    # SQL returns (min_lon, max_lon, min_lat, max_lat)
    # SpatialWindowIterator expects (lon_min, lat_min, lon_max, lat_max)
    return float(row[0]), float(row[2]), float(row[1]), float(row[3])


def _query_active_points_in_window(
    conn: Any,
    window: SpatialWindow,
    *,
    table: str = "trajectory_points",
) -> list[TrajectoryPoint]:
    """Query active trajectory points + injected points in a spatial window."""
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       source_id, source_row_key, extra "
            f"FROM {table} "
            f"WHERE is_active IS NOT FALSE "
            f"  AND lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s "
            f"ORDER BY vehicle_id, timestamp",
            (window.x_min, window.x_max, window.y_min, window.y_max),
        )
        for row in cur:
            rows.append({
                "vehicle_id": row[0],
                "timestamp": float(row[1]),
                "lon": float(row[2]),
                "lat": float(row[3]),
                "heading": float(row[4]),
                "speed": float(row[5]),
                "source_id": row[6],
                "source_row_key": row[7],
                "extra": row[8] if isinstance(row[8], dict) else {},
            })

    # Also query injected_points in same window
    # ZH: injected_points 没有 source_id 列，用 from_source_id/from_source_row_key 别名
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       from_source_id AS source_id, "
            f"       from_source_row_key AS source_row_key "
            f"FROM injected_points "
            f"WHERE lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s "
            f"ORDER BY vehicle_id, timestamp",
            (window.x_min, window.x_max, window.y_min, window.y_max),
        )
        for row in cur:
            rows.append({
                "vehicle_id": row[0],
                "timestamp": float(row[1]),
                "lon": float(row[2]),
                "lat": float(row[3]),
                "heading": float(row[4]),
                "speed": float(row[5]),
                "source_id": row[6],
                "source_row_key": row[7],
                "extra": {},
            })

    rows.sort(key=lambda r: (r["vehicle_id"], r["timestamp"]))
    return _build_points_from_rows(rows)


def _query_active_points_near_cluster(
    conn: Any,
    cluster: HotspotCluster,
    radius_m: float,
    *,
    table: str = "trajectory_points",
) -> list[TrajectoryPoint]:
    """Query active points within *radius_m* of a cluster centroid."""
    # Convert radius_m to approximate degrees
    radius_deg = radius_m / 111320.0
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       source_id, source_row_key, extra "
            f"FROM {table} "
            f"WHERE is_active IS NOT FALSE "
            f"  AND lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s "
            f"ORDER BY vehicle_id, timestamp",
            (
                cluster.centroid_lon - radius_deg,
                cluster.centroid_lon + radius_deg,
                cluster.centroid_lat - radius_deg,
                cluster.centroid_lat + radius_deg,
            ),
        )
        for row in cur:
            rows.append({
                "vehicle_id": row[0],
                "timestamp": float(row[1]),
                "lon": float(row[2]),
                "lat": float(row[3]),
                "heading": float(row[4]),
                "speed": float(row[5]),
                "source_id": row[6],
                "source_row_key": row[7],
                "extra": row[8] if isinstance(row[8], dict) else {},
            })

    # Also query injected_points near cluster
    # ZH: injected_points 没有 source_id 列，用 from_source_id/from_source_row_key 别名
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       from_source_id AS source_id, "
            f"       from_source_row_key AS source_row_key "
            f"FROM injected_points "
            f"WHERE lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s "
            f"ORDER BY vehicle_id, timestamp",
            (
                cluster.centroid_lon - radius_deg,
                cluster.centroid_lon + radius_deg,
                cluster.centroid_lat - radius_deg,
                cluster.centroid_lat + radius_deg,
            ),
        )
        for row in cur:
            rows.append({
                "vehicle_id": row[0],
                "timestamp": float(row[1]),
                "lon": float(row[2]),
                "lat": float(row[3]),
                "heading": float(row[4]),
                "speed": float(row[5]),
                "source_id": row[6],
                "source_row_key": row[7],
                "extra": {},
            })

    rows.sort(key=lambda r: (r["vehicle_id"], r["timestamp"]))
    return _build_points_from_rows(rows)


def _build_points_from_rows(rows: list[dict[str, Any]]) -> list[TrajectoryPoint]:
    """Convert DB row dicts to TrajectoryPoint objects."""
    points: list[TrajectoryPoint] = []
    for r in rows:
        points.append(TrajectoryPoint(
            point_ref=PointRef(
                source_id=r["source_id"],
                source_row_key=r["source_row_key"],
                stable=True,
            ),
            vehicle_id=r["vehicle_id"],
            timestamp=r["timestamp"],
            lon=r["lon"],
            lat=r["lat"],
            heading=r["heading"],
            speed=r["speed"],
            extra=r["extra"],
        ))
    return points


def _tracks_from_points(points: list[TrajectoryPoint]) -> dict[str, list[TrajectoryPoint]]:
    """Group points by vehicle_id into a tracks dict."""
    tracks: dict[str, list[TrajectoryPoint]] = {}
    for tp in points:
        if tp.vehicle_id not in tracks:
            tracks[tp.vehicle_id] = []
        tracks[tp.vehicle_id].append(tp)
    return tracks


def _detect_intersections_db(conn: Any, cfg: IntersectConfig) -> IntersectResult:
    """DB-mode intersection detection with spatial sliding windows.

    Queries active trajectory points from the database in overlapping
    spatial windows to keep in-memory footprint bounded.  Turn events
    and orthogonal candidates are collected globally (they are much
    smaller than the full point set), and G*/DBSCAN run on the full set.
    """

    # ── Query spatial extent ────────────────────────────────────────
    extent = _query_spatial_extent(conn, table=cfg.db_table)
    if extent is None:
        return IntersectResult(
            turn_events=[], hotspot_events=[], orthogonal_candidates=[],
            clusters=[], topologies=[],
            report=IntersectReport(
                track_count=0, turn_event_count=0, hotspot_event_count=0,
                orthogonal_candidate_count=0, cluster_count=0,
                cluster_count_turn=0, cluster_count_ortho=0,
                complete_intersection_count=0,
            ),
        )

    windows = SpatialWindowIterator(
        extent,
        size=cfg.spatial_window_size,
        overlap=cfg.spatial_window_overlap,
    )

    all_turn_events: list[TurnEvent] = []
    all_ortho_candidates: list[OrthogonalCandidate] = []
    origin_lon = (extent[0] + extent[2]) / 2.0
    origin_lat = (extent[1] + extent[3]) / 2.0

    t0 = time.perf_counter()
    window_count = 0
    total_points = 0
    print(f"  ── 空间滑窗 / Spatial windows ──")

    for window in windows.iter_windows():
        window_count += 1
        points = _query_active_points_in_window(conn, window, table=cfg.db_table)
        if not points:
            continue
        total_points += len(points)
        window_tracks = _tracks_from_points(points)

        # Turn detection per window
        if cfg.use_turn_events:
            window_turns = _detect_turn_events(window_tracks, origin_lon, origin_lat, cfg)
            # Re-index event_id globally
            for te in window_turns:
                all_turn_events.append(replace(te, event_id=len(all_turn_events)))

        # Orthogonal extraction per window
        if cfg.use_orthogonal_candidates:
            all_ortho_candidates.extend(
                _extract_orthogonal_candidates(window_tracks, origin_lon, origin_lat)
            )

    print(f"    ... {window_count} 窗/windows, {total_points:,} 点/points ({time.perf_counter() - t0:.1f}s)")

    # ── Global G* + DBSCAN ──────────────────────────────────────────
    hotspot_events: list[TurnEvent] = []
    all_clusters: list[HotspotCluster] = []

    if cfg.use_turn_events and all_turn_events:
        hotspot_events = _select_hotspot_events(all_turn_events, cfg)
        if hotspot_events:
            clusters_turn = _cluster_dbscan(
                hotspot_events, origin_lon, origin_lat,
                eps_m=cfg.dbscan_eps_m_turn,
                min_samples=cfg.dbscan_min_samples_turn,
                source="turn",
            )
            all_clusters.extend(clusters_turn)

    if cfg.use_orthogonal_candidates and all_ortho_candidates:
        clusters_ortho = _cluster_orthogonal_candidates(
            all_ortho_candidates, origin_lon, origin_lat,
            eps_m=cfg.dbscan_eps_m_ortho,
            min_samples=cfg.dbscan_min_samples_ortho,
        )
        all_clusters.extend(clusters_ortho)

    # ── Merge + reindex ─────────────────────────────────────────────
    merged_clusters = _merge_clusters(all_clusters, cfg.merge_centroid_distance_m,
                                       origin_lon, origin_lat)
    merged_clusters = [
        replace(c, cluster_id=new_id)
        for new_id, c in enumerate(merged_clusters)
    ]
    merged_clusters = _annotate_source_ablation(
        merged_clusters, hotspot_events, all_ortho_candidates,
        cfg, origin_lon, origin_lat,
    )

    # ── Topology verification (per cluster, from DB) ────────────────
    topologies: list[IntersectionTopology] = []
    annotated_clusters: list[HotspotCluster] = []
    all_observations: list[EntryExitObservation] = []
    total_clusters = len(merged_clusters)

    print(f"  ── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")
    for cluster in merged_clusters:
        radius = max(cfg.min_intersection_radius_m,
                     cluster.radius_m + cfg.topology_radius_padding_m)
        nearby_points = _query_active_points_near_cluster(
            conn, cluster, radius * 2.0, table=cfg.db_table,
        )
        nearby_tracks = _tracks_from_points(nearby_points)
        observations, crossing_points = _extract_entry_exit_observations_batch(
            nearby_tracks,
            cluster=cluster,
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            radius_m=radius,
            bearing_window=cfg.bearing_window,
        )
        # Attach crossing points to cluster for replay provenance (mirrors in-memory path)
        cluster = replace(cluster, crossing_points=tuple(crossing_points))
        topology, annotated, cluster_observations = _build_topology_for_cluster(
            cluster, observations, origin_lon, origin_lat, cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_observations)

    # ── Report ───────────────────────────────────────────────────────
    turn_clusters = sum(1 for c in annotated_clusters if "turn" in _cluster_sources(c))
    ortho_clusters = sum(1 for c in annotated_clusters if "orthogonal" in _cluster_sources(c))
    source_report = _complete_source_report(annotated_clusters, topologies)

    report = IntersectReport(
        track_count=0,  # Unknown in DB mode
        turn_event_count=len(all_turn_events),
        hotspot_event_count=len(hotspot_events),
        orthogonal_candidate_count=len(all_ortho_candidates),
        cluster_count=len(annotated_clusters),
        cluster_count_turn=turn_clusters,
        cluster_count_ortho=ortho_clusters,
        complete_intersection_count=sum(1 for t in topologies if t.is_complete),
        complete_intersection_source_counts=source_report["source_counts"],
        complete_intersection_source_ratios=source_report["source_ratios"],
        complete_intersection_critical_source_counts=source_report["critical_source_counts"],
        complete_intersection_critical_source_ratios=source_report["critical_source_ratios"],
    )

    return IntersectResult(
        turn_events=all_turn_events,
        hotspot_events=hotspot_events,
        orthogonal_candidates=all_ortho_candidates,
        clusters=annotated_clusters,
        topologies=topologies,
        observations=all_observations,
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
    total_tracks = len(tracks)
    t0 = time.perf_counter()
    last_t = t0
    track_count = 0
    print(f"  ── 转向事件检测 / Turn event detection ({total_tracks:,} 轨迹/tracks) ...")

    for vehicle_id, track in tracks.items():
        if len(track) < 2 * w + 1:
            continue

        xy = [_to_xy(p.lon, p.lat, origin_lon, origin_lat) for p in track]
        # 直接使用 GPS heading 作为逐段方位角，避免 O(w*(n-2w)) 次 atan2
        headings = [p.heading for p in track]

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

            in_angles = headings[i - w : i]
            out_angles = headings[i : i + w]

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
                        source="turn",
                        point_ref=p.point_ref,
                    )
                )

        track_count += 1
        now = time.perf_counter()
        if track_count % 100 == 0 or (now - last_t) >= 3.0:
            print(f"    ... 已处理 / Processed {track_count:,}/{total_tracks:,} 轨迹/tracks ({now - t0:.1f}s)")
            last_t = now

    print(f"  ── 转向事件检测: {len(events):,} 事件/events ({time.perf_counter() - t0:.1f}s)")
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

    t0 = time.perf_counter()
    last_t = t0
    print(f"    ... 计算 Getis-Ord G* / Computing G* ({n:,} 事件/events) ...")
    local_counts: list[float] = []
    for i, event in enumerate(events):
        count = 0.0
        ex, ey = event.x, event.y
        for other in events:
            dx = other.x - ex
            dy = other.y - ey
            if dx * dx + dy * dy <= radius_m * radius_m:
                count += 1.0
        local_counts.append(count)
        now = time.perf_counter()
        if (i + 1) % 500 == 0 or (now - last_t) >= 3.0:
            print(f"      ... G*: {i + 1:,}/{n:,} 事件/events ({now - t0:.1f}s)")
            last_t = now

    print(f"    ... G*: {n:,} 事件/events ({time.perf_counter() - t0:.1f}s)")

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

    print(f"  ── 热点筛选 / Hotspot selection ({len(events):,} 事件/events) ...")
    t0 = time.perf_counter()
    z_scores = _compute_getis_ord_g_star(events, cfg.g_star_radius_m)
    result = [
        e
        for e in events
        if z_scores.get(e.event_id, 0.0) >= cfg.g_star_min_z
        and abs(e.turn_deg) >= cfg.min_turn_deg
    ]
    print(f"  ── 热点筛选: {len(result):,} 事件/events ({time.perf_counter() - t0:.1f}s)")
    return result


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
    t0 = time.perf_counter()
    last_t = t0

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
        now = time.perf_counter()
        if i % 500 == 0 or (now - last_t) >= 3.0:
            print(f"      ... DBSCAN: {i + 1:,}/{n:,} 点/points, {cluster_id} 簇/clusters ({now - t0:.1f}s)")
            last_t = now

    print(f"    ... DBSCAN: {n:,} 点/points → {cluster_id:,} 簇/clusters ({time.perf_counter() - t0:.1f}s)")
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
    print(f"    ... DBSCAN 聚类 ({source}) / DBSCAN clustering ({n:,} 点/points) ...")
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
        cluster = HotspotCluster(
            cluster_id=cid,
            centroid_x=cx,
            centroid_y=cy,
            centroid_lon=lon,
            centroid_lat=lat,
            radius_m=radius,
            cluster_ref=f"cluster_{source}_{cid}",
            events=tuple(cluster_items),
            source=source,
        )
        clusters.append(
            _with_source_provenance(
                cluster,
                source_counts={source: len(cluster_items)},
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
    print(f"    ... DBSCAN 聚类 (orthogonal) / DBSCAN clustering ({n:,} 点/points) ...")
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
                source="orthogonal",
                point_ref=c.point_ref,
            )
            for i, c in enumerate(cluster_items)
        )
        cluster = HotspotCluster(
            cluster_id=cid,
            centroid_x=cx,
            centroid_y=cy,
            centroid_lon=lon,
            centroid_lat=lat,
            radius_m=radius,
            cluster_ref=f"cluster_orthogonal_{cid}",
            events=cluster_events,
            source="orthogonal",
        )
        clusters.append(
            _with_source_provenance(
                cluster,
                source_counts={"orthogonal": len(cluster_items)},
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
        merged_events = list(ci.events)
        merged_source_counts = _cluster_source_counts(ci)
        merged_source_refs = _cluster_source_point_refs(ci)
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
                    merged_events.extend(cj.events)
                    merged_source_counts = _merge_count_dicts(
                        merged_source_counts,
                        _cluster_source_counts(cj),
                    )
                    merged_source_refs = _merge_ref_dicts(
                        merged_source_refs,
                        _cluster_source_point_refs(cj),
                    )
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
        merged_cluster = HotspotCluster(
            cluster_id=ci.cluster_id,
            centroid_x=new_cx,
            centroid_y=new_cy,
            centroid_lon=new_lon,
            centroid_lat=new_lat,
            radius_m=merged_radius,
            cluster_ref=ci.cluster_ref,  # keep absorbing cluster's ref
            events=tuple(merged_events),
            source=_source_label(tuple(sorted(merged_source_counts))),
        )
        kept.append(
            _with_source_provenance(
                merged_cluster,
                source_counts=merged_source_counts,
                source_point_refs=merged_source_refs,
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


def _build_topology_for_cluster(
    cluster: HotspotCluster,
    observations: list[EntryExitObservation],
    origin_lon: float,
    origin_lat: float,
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
        cluster_id=cluster.cluster_id,
        branch_centers_deg=branch_centers,
        transition_counts=transition_counts,
        observation_count=len(observations),
        is_complete=is_complete,
        reason=reason,
    )
    return topology, cluster, observations


def _build_intersection_topologies(
    clusters: list[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
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
    print(f"  ── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")

    for cluster in clusters:
        cluster_idx = len(topologies) + 1
        print(f"    ... 簇 / Cluster {cluster_idx:,}/{total_clusters:,} ({time.perf_counter() - t0:.1f}s)")
        radius = max(
            cfg.min_intersection_radius_m,
            cluster.radius_m + cfg.topology_radius_padding_m,
        )
        observations, crossing_points = _extract_entry_exit_observations_batch(
            tracks,
            cluster=cluster,
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            radius_m=radius,
            bearing_window=cfg.bearing_window,
        )
        # Attach crossing points to cluster for replay provenance
        cluster = replace(cluster, crossing_points=tuple(crossing_points))

        topology, annotated, cluster_obs = _build_topology_for_cluster(
            cluster, observations, origin_lon, origin_lat, cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_obs)

    print(f"  ── 拓扑验证: {len(topologies):,} 拓扑/topologies ({time.perf_counter() - t0:.1f}s)")
    return topologies, annotated_clusters, all_observations


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
        return [], []

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

    # ── Collect all crossing points (for replay provenance) ─────
    crossing_counter = 0
    crossing_points: list[CrossingPoint] = []
    for flat_idx, (vehicle_id, seg_idx) in enumerate(segment_meta):
        if not hit_mask[flat_idx]:
            continue
        seg_start_pt = track_lookup[vehicle_id][seg_idx]
        is_entry = bool(entry_mask[flat_idx])
        # Convert crossing xy → lon/lat
        cx_m, cy_m = float(crossing_xy_np[flat_idx][0]), float(crossing_xy_np[flat_idx][1])
        clon, clat = _to_lonlat(cx_m, cy_m, origin_lon, origin_lat)
        crossing_points.append(
            CrossingPoint(
                crossing_id=crossing_counter,
                cluster_ref=cluster.cluster_ref,
                vehicle_id=vehicle_id,
                point_ref=seg_start_pt.point_ref,
                segment_index=seg_idx,
                crossing_lon=clon,
                crossing_lat=clat,
                is_entry=is_entry,
            )
        )
        crossing_counter += 1

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
    return observations, crossing_points
