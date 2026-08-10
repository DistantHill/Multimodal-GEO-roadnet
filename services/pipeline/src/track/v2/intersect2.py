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
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .logger import LiveStatus

from .logger import log_info

from ._perf import PerfTracker, TimedStage, timed_iter, track_for_loops

from ._geometry import (
    _angle_delta_deg,
    _batch_segment_intersections,
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
    CrossTrackCandidate,
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


def _perf_iter(perf, iterable, *, name, total=None, track=True):
    """Wrap ``iterable`` with per-iteration timing when a ``perf`` tracker
    is supplied; otherwise pass through untouched (zero-overhead).

    ZH: 有 ``perf`` 追踪器时对迭代逐次计时；否则原样直通（零开销）。
        ``timed_iter`` 内部的迭代深度上限在超过 3 层时同样会自动退化。
    EN: Time each iteration via ``perf`` when a tracker is present; otherwise
        return the raw iterable. ``timed_iter`` itself auto-passthroughs
        past the iteration-depth limit.
    """
    if perf is not None:
        return perf.timed_iter(iterable, name=name, total=total, track=track)
    return iterable


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

    # ── Cross-track orthogonal detection (Method 3) ──────────────────
    cross_track_straightness_deg: float = 15.0
    cross_track_min_angle_deg: float = 80.0
    cross_track_max_angle_deg: float = 100.0
    cross_track_dbscan_eps_m: float = 100.0
    cross_track_dbscan_min_samples: int = 3

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
    source_id: str | None = None
    run_id: str | None = None
    spatial_window_size: tuple[float, float] = (2000.0, 2000.0)
    spatial_window_overlap: float = 0.15

    # ── Live status display (optional, set by CLI for dynamic terminal output) ──
    live_status: Any = None


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
    cross_track_candidates: list[CrossTrackCandidate] = field(default_factory=list)

    def get_report(self) -> IntersectReport:
        return self.report


def _ratio_dict(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    return {source: count / total for source, count in sorted(counts.items())}


def _cluster_sources(cluster: HotspotCluster) -> tuple[str, ...]:
    if cluster.algorithm_sources:
        return cluster.algorithm_sources
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
    source_ids = set(cluster.source_ids)
    for event in cluster.events:
        source_ids.update(source_id for source_id in event.source_ids if source_id)
    for point_refs in refs.values():
        source_ids.update(ref.source_id for ref in point_refs if ref.source_id)
    return replace(
        cluster,
        source=_source_label(sources),
        algorithm_sources=sources,
        source_ids=tuple(sorted(source_ids)),
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
    perf: PerfTracker | None = None,
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
                perf=perf,
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
                perf=perf,
            )
        )
    return _merge_clusters(clusters, cfg.merge_centroid_distance_m, origin_lon, origin_lat, perf=perf)


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
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[HotspotCluster]:
    if not clusters:
        return []

    all_sources = sorted({source for cluster in clusters for source in _cluster_sources(cluster)})
    report_ablation = len(all_sources) > 1  # only print ablation progress when there are multiple sources
    if report_ablation:
        log_info(f"    源消融分析 / Source ablation ({len(all_sources)} 源/sources) ...")
        t_abl = time.perf_counter()
    reclustered_without: dict[str, list[HotspotCluster]] = {}
    for idx, source in enumerate(_perf_iter(perf, all_sources, name="ablation_sources")):
        if report_ablation:
            log_info(f"      消融 / Ablating: {source} ...")
            t1 = time.perf_counter()
        reclustered_without[source] = _build_candidate_clusters(
            hotspot_events,
            orthogonal_candidates,
            cfg,
            origin_lon,
            origin_lat,
            disabled_sources={source},
            perf=perf,
        )
        if report_ablation:
            log_info(f"      消融 / Ablated {source}: {len(reclustered_without[source])} 簇/clusters ({time.perf_counter() - t1:.1f}s)")
            if live_status is not None:
                _route_progress(
                    live_status,
                    detail=f"消融/Ablated {source}:\t{len(reclustered_without[source])}\t簇/clusters",
                    progress=(idx + 1) / len(all_sources),
                    elapsed=time.perf_counter() - t_abl,
                    stage="Intersect2: 源消融 / Source Ablation",
                )

    annotated: list[HotspotCluster] = []
    for cluster in _perf_iter(perf, clusters, name="ablation_clusters"):
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
        log_info(f"    源消融分析: {len(annotated):,} 簇/clusters ({time.perf_counter() - t_abl:.1f}s)")
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


# ── Shared pipeline helpers (used by both handle and DB modes) ──────────


def _route_progress(
    live_status: Any,
    *,
    detail: str,
    progress: float | None,
    elapsed: float,
    sub_detail: str = "",
    stage: str | None = None,
) -> None:
    """ZH: 进度更新统一出口——路由到 live_status 面板或 print 回退。
    EN: Single routing point for progress updates → live_status panel or print fallback."""
    if live_status is not None:
        live_status.update(
            progress=progress,
            detail=detail,
            sub_detail=sub_detail,
            elapsed=elapsed,
            stage=stage,
        )
    else:
        print(f"    ... {detail} ({elapsed:.1f}s)")


def _empty_intersect_result() -> IntersectResult:
    """Common empty result for both modes when no data is available."""
    return IntersectResult(
        turn_events=[], hotspot_events=[], orthogonal_candidates=[],
        clusters=[], topologies=[],
        report=IntersectReport(
            track_count=0, turn_event_count=0, hotspot_event_count=0,
            orthogonal_candidate_count=0, cross_track_candidate_count=0,
            cluster_count=0, cluster_count_turn=0, cluster_count_ortho=0,
            cluster_count_cross_track=0, complete_intersection_count=0,
        ),
        cross_track_candidates=[],
    )


def _build_intersect_report(
    annotated_clusters: list[HotspotCluster],
    topologies: list[IntersectionTopology],
    turn_events: list[TurnEvent],
    hotspot_events: list[TurnEvent],
    ortho_candidates: list[OrthogonalCandidate],
    cross_candidates: list[CrossTrackCandidate],
    track_count: int,
) -> IntersectReport:
    """Shared report construction for both modes."""
    turn_clusters = sum(1 for c in annotated_clusters if "turn" in _cluster_sources(c))
    ortho_clusters = sum(1 for c in annotated_clusters if "orthogonal" in _cluster_sources(c))
    cross_clusters = sum(1 for c in annotated_clusters if "orthogonal_cross_track" in _cluster_sources(c))
    source_report = _complete_source_report(annotated_clusters, topologies)
    return IntersectReport(
        track_count=track_count,
        turn_event_count=len(turn_events),
        hotspot_event_count=len(hotspot_events),
        orthogonal_candidate_count=len(ortho_candidates),
        cross_track_candidate_count=len(cross_candidates),
        cluster_count=len(annotated_clusters),
        cluster_count_turn=turn_clusters,
        cluster_count_ortho=ortho_clusters,
        cluster_count_cross_track=cross_clusters,
        complete_intersection_count=sum(1 for t in topologies if t.is_complete),
        complete_intersection_source_counts=source_report["source_counts"],
        complete_intersection_source_ratios=source_report["source_ratios"],
        complete_intersection_critical_source_counts=source_report["critical_source_counts"],
        complete_intersection_critical_source_ratios=source_report["critical_source_ratios"],
    )


def _cluster_pipeline(
    turn_events: list[TurnEvent],
    ortho_candidates: list[OrthogonalCandidate],
    cross_candidates: list[CrossTrackCandidate],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[TurnEvent], list[HotspotCluster]]:
    """Shared G* + DBSCAN ×3 + merge + reindex + ablation for both modes."""
    all_clusters: list[HotspotCluster] = []
    hotspot_events: list[TurnEvent] = []

    # ── Method 1: Turn event → G* → DBSCAN ────────────────────────
    if cfg.use_turn_events and turn_events:
        hotspot_events = _select_hotspot_events(turn_events, cfg, live_status=live_status, perf=perf)
        if hotspot_events:
            all_clusters.extend(_cluster_dbscan(
                hotspot_events, origin_lon, origin_lat,
                eps_m=cfg.dbscan_eps_m_turn,
                min_samples=cfg.dbscan_min_samples_turn,
                source="turn",
                live_status=live_status,
                perf=perf,
            ))

    # ── Method 2: Orthogonal → DBSCAN ──────────────────────────────
    if cfg.use_orthogonal_candidates and ortho_candidates:
        all_clusters.extend(_cluster_orthogonal_candidates(
            ortho_candidates, origin_lon, origin_lat,
            eps_m=cfg.dbscan_eps_m_ortho,
            min_samples=cfg.dbscan_min_samples_ortho,
            live_status=live_status,
            perf=perf,
        ))

    # ── Method 3: Cross-track → DBSCAN ─────────────────────────────
    if cfg.use_orthogonal_candidates and cross_candidates:
        all_clusters.extend(_cluster_orthogonal_cross_track_candidates(
            cross_candidates, origin_lon, origin_lat,
            eps_m=cfg.cross_track_dbscan_eps_m,
            min_samples=cfg.cross_track_dbscan_min_samples,
            live_status=live_status,
            perf=perf,
        ))

    # ── Merge + reindex + ablation ─────────────────────────────────
    merged = _merge_clusters(all_clusters, cfg.merge_centroid_distance_m,
                              origin_lon, origin_lat, perf=perf)
    merged = [replace(c, cluster_id=i) for i, c in enumerate(merged)]
    annotated = _annotate_source_ablation(
        merged, hotspot_events, ortho_candidates,
        cfg, origin_lon, origin_lat,
        live_status=live_status,
        perf=perf,
    )
    return hotspot_events, annotated


def _extract_methods_123(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[TurnEvent], list[OrthogonalCandidate], list[CrossTrackCandidate]]:
    """Run all three candidate extraction methods on a tracks dict (handle mode)."""
    turn_events = _detect_turn_events(tracks, origin_lon, origin_lat, cfg, live_status=live_status, perf=perf) if cfg.use_turn_events else []
    ortho_candidates: list[OrthogonalCandidate] = []
    cross_candidates: list[CrossTrackCandidate] = []
    if cfg.use_orthogonal_candidates:
        ortho_candidates = _extract_orthogonal_candidates(tracks, origin_lon, origin_lat, live_status=live_status, perf=perf)
        cross_candidates, _, _ = _extract_orthogonal_cross_track_candidates(tracks, origin_lon, origin_lat, cfg, live_status=live_status, perf=perf)
    return turn_events, ortho_candidates, cross_candidates


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
      - **Handle mode**: ``source`` is a ``TrajectorySourceHandle``.
        All points are read into memory and grouped by vehicle_id.
      - **DB mode**: ``conn`` is a psycopg connection and
        ``config.query_active_from_db`` is True.  Points are queried from
        the database in spatial sliding windows, dramatically reducing
        in-memory footprint for large datasets.
    """
    cfg = config or IntersectConfig()

    if conn is not None and cfg.query_active_from_db:
        return _detect_intersections_db(conn, cfg)
    elif source is not None:
        return _detect_intersections_handle(source, cfg)
    else:
        raise V2Error(
            "detect_intersections: provide either 'source' (handle mode) "
            "or 'conn' + query_active_from_db (DB mode)"
        )


def _detect_intersections_handle(
    source: TrajectorySourceHandle,
    cfg: IntersectConfig,
) -> IntersectResult:
    tracks, origin_lon, origin_lat = _group_by_vehicle(source)
    if not tracks:
        return _empty_intersect_result()

    live = cfg.live_status
    own_live = False
    if live is None:
        # ZH: Handle 模式下如果没有外部 LiveStatus，自建一个。
        # EN: If no external LiveStatus in handle mode, create our own.
        try:
            from .logger import LiveStatus
            live = LiveStatus("Intersect2: 交叉口检测 / Intersection Detection")
            live.start()
            own_live = True
        except Exception:
            live = None
    tracker = PerfTracker("intersect2_handle")
    try:
        with tracker.stage("extract_methods_123"):
            turn_events, ortho_candidates, cross_candidates = _extract_methods_123(
                tracks, origin_lon, origin_lat, cfg,
                live_status=live, perf=tracker,
            )

        with tracker.stage("cluster_pipeline"):
            hotspot_events, annotated_clusters = _cluster_pipeline(
                turn_events, ortho_candidates, cross_candidates,
                origin_lon, origin_lat, cfg,
                live_status=live, perf=tracker,
            )

        with tracker.stage("topology"):
            topologies, annotated_clusters, observations = _build_intersection_topologies(
                annotated_clusters, tracks, origin_lon, origin_lat, cfg,
                live_status=live, perf=tracker,
            )

        log_info(tracker.report())
        return IntersectResult(
            turn_events=turn_events,
            hotspot_events=hotspot_events,
            orthogonal_candidates=ortho_candidates,
            clusters=annotated_clusters,
            topologies=topologies,
            observations=observations,
            cross_track_candidates=cross_candidates,
            report=_build_intersect_report(
                annotated_clusters, topologies,
                turn_events, hotspot_events, ortho_candidates, cross_candidates,
                track_count=len(tracks),
            ),
        )
    finally:
        if own_live and live is not None:
            live.stop()


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

    def count_windows(self) -> int:
        """Return the number of windows that :meth:`iter_windows` will yield."""
        if self._lon_max <= self._lon_min or self._lat_max <= self._lat_min:
            return 0
        lon_count = math.ceil((self._lon_max - self._lon_min) / self._step_lon)
        lat_count = math.ceil((self._lat_max - self._lat_min) / self._step_lat)
        return lon_count * lat_count


def _query_spatial_extent(
    conn: Any,
    *,
    table: str = "trajectory_points",
    source_id: str | None = None,
) -> tuple[float, float, float, float] | None:
    """Query MIN/MAX lon/lat of active trajectory points.

    Returns ``(lon_min, lat_min, lon_max, lat_max)`` compatible with
    ``SpatialWindowIterator``.
    """
    sql = f"SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) FROM {table} WHERE is_active IS NOT FALSE"
    with conn.cursor() as cur:
        cur.execute(sql)
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
    source_id: str | None = None,
    run_id: str | None = None,
) -> list[TrajectoryPoint]:
    """Query active trajectory points + injected points in a spatial window.

    ZH: 使用 PostGIS GiST 空间索引 (geom && ST_MakeEnvelope) 替代
        lon/lat BETWEEN，走 R-tree 2D 索引扫描。UNION ALL 合并两表一次往返。
    """
    rows: list[dict[str, Any]] = []
    injected_run_clause = " AND run_id = %s" if run_id is not None else ""
    params: list[Any] = [source_id or ""]
    params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
    params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
    if run_id is not None:
        params.append(run_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       source_id, source_row_key, extra, "
            f"       NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
            f"       ''::text AS generated_reason "
            f"FROM ("
            f"  SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"         %s::text AS source_id, id::text AS source_row_key, extra, "
            f"         NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
            f"         ''::text AS generated_reason "
            f"  FROM {table} "
            f"  WHERE is_active IS NOT FALSE "
            f"    AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
            f"  UNION ALL "
            f"  SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"         from_source_id AS source_id, "
            f"         from_source_row_key AS source_row_key, "
            f"         '{{}}'::jsonb AS extra, "
            f"         from_source_row_key, to_source_row_key, generated_reason "
            f"  FROM injected_points "
            f"  WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326){injected_run_clause} "
            f") AS combined "
            f"ORDER BY vehicle_id, timestamp",
            params,
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
                "from_source_row_key": row[9],
                "to_source_row_key": row[10],
                "generated_reason": row[11],
            })

    # DB ORDER BY 已保证 (vehicle_id, timestamp) 有序，无需 Python 侧再排
    return _build_points_from_rows(rows)


def _stream_vehicle_tracks_from_db(
    conn: Any,
    *,
    table: str = "trajectory_points",
    source_id: str | None = None,
    run_id: str | None = None,
) -> Iterator[list[TrajectoryPoint]]:
    """Stream tracks one vehicle at a time from DB (global, no spatial filter).

    ZH: 从 DB 逐车流式读取轨迹点。使用 UNION ALL 合并 active points 和
        injected_points，按 (vehicle_id, timestamp) 排序后检测 vehicle_id
        变化来分组。内存占用 = 单车辆最大点数。
    """
    injected_run_clause = " WHERE run_id = %s" if run_id is not None else ""
    params: list[Any] = [source_id or ""]
    if run_id is not None:
        params.append(run_id)
    # ZH: 服务器端命名游标 — PG 按需 FETCH，避免全量物化到 portal。
    # EN: Server-side named cursor — FETCHes on demand, no full portal materialization.
    with conn.cursor(
        name=f"stream_tracks_{uuid.uuid4().hex}",
    ) as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       %s::text AS source_id, id::text AS source_row_key, extra, "
            f"       NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
            f"       ''::text AS generated_reason "
            f"FROM {table} "
            f"WHERE is_active IS NOT FALSE "
            f"UNION ALL "
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       from_source_id, from_source_row_key, '{{}}'::jsonb, "
            f"       from_source_row_key, to_source_row_key, generated_reason "
            f"FROM injected_points{injected_run_clause} "
            f"ORDER BY vehicle_id, timestamp",
            params,
        )
        current_vid: str | None = None
        current_track: list[TrajectoryPoint] = []
        for row in cur:
            vid = str(row[0])
            if current_vid is not None and vid != current_vid:
                yield current_track
                current_track = []
            current_vid = vid
            current_track.append(TrajectoryPoint(
                point_ref=PointRef(
                    source_id=str(row[6] or ""),
                    source_row_key=str(row[7] or ""),
                    stable=not bool(row[11]),
                    from_ref=str(row[9]) if row[9] is not None else None,
                    to_ref=str(row[10]) if row[10] is not None else None,
                    generated_reason=str(row[11] or ""),
                ),
                vehicle_id=vid,
                timestamp=float(row[1]),
                lon=float(row[2]),
                lat=float(row[3]),
                heading=float(row[4]),
                speed=float(row[5]),
                extra=row[8] if isinstance(row[8], dict) else {},
            ))
        if current_track:
            yield current_track


def _query_active_points_near_cluster(
    conn: Any,
    cluster: HotspotCluster,
    radius_m: float,
    *,
    table: str = "trajectory_points",
    source_id: str | None = None,
    run_id: str | None = None,
) -> list[TrajectoryPoint]:
    """Query active points within *radius_m* of a cluster centroid."""
    # Convert radius_m to approximate degrees
    radius_deg = radius_m / 111320.0
    rows: list[dict[str, Any]] = []
    raw_params: list[Any] = [source_id or "",
        cluster.centroid_lon - radius_deg,
        cluster.centroid_lon + radius_deg,
        cluster.centroid_lat - radius_deg,
        cluster.centroid_lat + radius_deg,
    ]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       %s::text AS source_id, id::text AS source_row_key, extra, "
            f"       NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
            f"       ''::text AS generated_reason "
            f"FROM {table} "
            f"WHERE is_active IS NOT FALSE "
            f"  AND lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s "
            f"ORDER BY vehicle_id, timestamp",
            raw_params,
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
                "from_source_row_key": row[9],
                "to_source_row_key": row[10],
                "generated_reason": row[11],
            })

    # Also query injected_points near cluster
    # ZH: injected_points 没有 source_id 列，用 from_source_id/from_source_row_key 别名
    injected_run_clause = " AND run_id = %s" if run_id is not None else ""
    injected_params: list[Any] = [
        cluster.centroid_lon - radius_deg,
        cluster.centroid_lon + radius_deg,
        cluster.centroid_lat - radius_deg,
        cluster.centroid_lat + radius_deg,
    ]
    if run_id is not None:
        injected_params.append(run_id)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       from_source_id AS source_id, "
            f"       from_source_row_key AS source_row_key, "
            f"       '{{}}'::jsonb AS extra, "
            f"       from_source_row_key, to_source_row_key, generated_reason "
            f"FROM injected_points "
            f"WHERE lon BETWEEN %s AND %s "
            f"  AND lat BETWEEN %s AND %s{injected_run_clause} "
            f"ORDER BY vehicle_id, timestamp",
            injected_params,
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
                "from_source_row_key": row[9],
                "to_source_row_key": row[10],
                "generated_reason": row[11],
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
                stable=not bool(r.get("generated_reason")),
                from_ref=(
                    str(r["from_source_row_key"])
                    if r.get("from_source_row_key") is not None else None
                ),
                to_ref=(
                    str(r["to_source_row_key"])
                    if r.get("to_source_row_key") is not None else None
                ),
                generated_reason=str(r.get("generated_reason") or ""),
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


# ═══════════════════════════════════════════════════════════════════════
# DB-mode helper functions
# ═══════════════════════════════════════════════════════════════════════


def _stream_extract_methods_1_2(
    conn: Any, cfg: IntersectConfig,
    origin_lon: float, origin_lat: float,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[TurnEvent], list[OrthogonalCandidate]]:
    """Stream per-vehicle from DB, batch 100 vehicles per Method 1/2 call.

    ZH: 外层 ``for track in _stream_vehicle_tracks_from_db(...)`` 是生成器流，
        逐迭代计时噪声大，整体由调用方 ``tracker.stage("stream_m1_m2")`` 计时。
    EN: The outer per-track generator stream is timed as a whole by the caller's
        ``tracker.stage("stream_m1_m2")`` — per-iteration timing would be noisy.
    """
    turn_events: list[TurnEvent] = []
    ortho_candidates: list[OrthogonalCandidate] = []
    t_total = time.perf_counter()
    last_progress_t = t_total
    track_count = 0
    batch: dict[str, list[TrajectoryPoint]] = {}
    BATCH_SIZE = 100

    for track in _stream_vehicle_tracks_from_db(
        conn,
        table=cfg.db_table,
        source_id=cfg.source_id,
        run_id=cfg.run_id,
    ):
        vid = track[0].vehicle_id
        batch[vid] = track
        track_count += 1

        if len(batch) >= BATCH_SIZE:
            # ── Flush batch ──
            if cfg.use_turn_events:
                for te in _detect_turn_events(batch, origin_lon, origin_lat, cfg, perf=perf):
                    turn_events.append(replace(te, event_id=len(turn_events)))
            if cfg.use_orthogonal_candidates:
                ortho_candidates.extend(
                    _extract_orthogonal_candidates(batch, origin_lon, origin_lat, perf=perf)
                )
            batch.clear()

            now = time.perf_counter()
            if (now - last_progress_t) >= 3.0:
                if live_status is not None:
                    live_status.update(
                        detail=f"流式/Streaming:\t{track_count:,}\t轨迹/tracks,\t"
                               f"{len(turn_events):,}\t转向/turn,\t{len(ortho_candidates):,}\t正交/ortho",
                        elapsed=now - t_total,
                        stage="Intersect2: 流式 / Stream M1+M2",
                    )
                last_progress_t = now

    # ── Flush remaining ──
    if batch:
        if cfg.use_turn_events:
            for te in _detect_turn_events(batch, origin_lon, origin_lat, cfg, perf=perf):
                turn_events.append(replace(te, event_id=len(turn_events)))
        if cfg.use_orthogonal_candidates:
            ortho_candidates.extend(
                _extract_orthogonal_candidates(batch, origin_lon, origin_lat, perf=perf)
            )

    log_info(f"── 流式处理: {track_count:,} 轨迹/tracks → "
             f"{len(turn_events):,} 转向事件/turn events, "
             f"{len(ortho_candidates):,} 正交候选/ortho candidates "
             f"({time.perf_counter() - t_total:.1f}s)")
    return turn_events, ortho_candidates


def _extract_cross_track_windows(
    conn: Any, cfg: IntersectConfig,
    extent: tuple[float, float, float, float],
    origin_lon: float, origin_lat: float,
    live_status: Any = None,
    *,
    perf: PerfTracker | None = None,
) -> list[CrossTrackCandidate]:
    """Method 3: spatial sliding windows for cross-track segment intersection."""
    candidates: list[CrossTrackCandidate] = []
    seen_segment_pairs: set[tuple[str, str, str, str, str]] = set()
    if not cfg.use_orthogonal_candidates:
        return candidates

    windows = SpatialWindowIterator(
        extent,
        size=cfg.spatial_window_size,
        overlap=cfg.spatial_window_overlap,
    )
    t0 = time.perf_counter()
    window_count = 0
    total_points = 0
    total_windows = windows.count_windows()
    last_progress_t = t0
    last_cross_sub = ""
    log_info(f"── 空间滑窗 / Spatial windows (Method 3: orthogonal_cross_track) ──")

    for window in _perf_iter(perf, windows.iter_windows(), name="spatial_windows", total=total_windows):
        window_count += 1
        now = time.perf_counter()

        # 1.1 — update live_status on EVERY window switch (no throttle)
        if live_status is not None and total_windows > 0:
            live_status.update(
                progress=window_count / total_windows,
                detail=f"当前窗口/Current window:\t{window_count:,}/{total_windows:,},\t"
                       f"{total_points:,}\tpts in window",
                sub_detail=last_cross_sub,
                elapsed=now - t0,
                stage="Intersect2: 空间滑窗 / Spatial Windows",
            )
        else:
            # print fallback (tests/benchmarks): keep old throttle
            if (window_count == 1 or window_count == total_windows
                    or window_count % 10 == 0 or (now - last_progress_t) >= 3.0):
                print(f"    ... 当前窗口/Current window:\t{window_count:,}/{total_windows:,},\t"
                      f"{total_points:,} pts ({now - t0:.1f}s)")
                last_progress_t = now

        points = _query_active_points_in_window(
            conn,
            window,
            table=cfg.db_table,
            source_id=cfg.source_id,
            run_id=cfg.run_id,
        )
        if not points:
            log_info(f"Window {window_count} Done, 累计处理点数/sum points: {total_points:,}")
            continue

        total_points += len(points)
        window_tracks = _tracks_from_points(points)
        win_cands, w_pairs, w_total = _extract_orthogonal_cross_track_candidates(
            window_tracks, origin_lon, origin_lat, cfg,
            live_status=live_status, sub_detail=True, perf=perf,
        )
        added = 0
        for candidate in win_cands:
            key = _cross_track_candidate_key(candidate)
            if key not in seen_segment_pairs:
                seen_segment_pairs.add(key)
                candidates.append(candidate)
                added += 1

        last_cross_sub = (
            f"跨车求交/Cross-track:\t{w_pairs:,}/{w_total:,}\t车辆对/pairs,\t"
            f"{len(candidates):,}\t候选/candidates"
        )
        # 1.3 — window done → log, not print
        log_info(f"Window {window_count} Done, 累计处理点数/sum points: {total_points:,}, "
                 f"{last_cross_sub}")

    log_info(f"    {window_count} 窗/windows, {total_points:,} 点/points "
             f"→ {len(candidates):,} 候选/candidates ({time.perf_counter() - t0:.1f}s)")
    return candidates


def _cross_track_candidate_key(
    candidate: CrossTrackCandidate,
) -> tuple[str, str, str, str, str]:
    """Stable identity for one canonical A/B segment-pair intersection."""
    return (
        candidate.vehicle_id,
        candidate.from_source_id,
        candidate.from_source_row_key,
        candidate.to_source_id,
        candidate.to_source_row_key,
    )


def _build_topologies_from_db(
    conn: Any,
    clusters: list[HotspotCluster],
    cfg: IntersectConfig,
    origin_lon: float,
    origin_lat: float,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[IntersectionTopology], list[HotspotCluster], list[EntryExitObservation]]:
    """Topology verification by querying nearby points from DB per cluster."""
    topologies: list[IntersectionTopology] = []
    annotated_clusters: list[HotspotCluster] = []
    all_observations: list[EntryExitObservation] = []
    total_clusters = len(clusters)

    log_info(f"── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")
    t0 = time.perf_counter()
    cluster_idx = 0
    for cluster in _perf_iter(perf, clusters, name="topo_clusters", total=total_clusters):
        cluster_idx += 1
        _route_progress(
            live_status,
            detail=f"簇/Cluster:\t{cluster_idx:,}/{total_clusters:,}",
            progress=cluster_idx / total_clusters,
            elapsed=time.perf_counter() - t0,
            stage="Intersect2: 拓扑验证 / Topology",
        )
        radius = max(cfg.min_intersection_radius_m,
                     cluster.radius_m + cfg.topology_radius_padding_m)
        nearby_points = _query_active_points_near_cluster(
            conn,
            cluster,
            radius * 2.0,
            table=cfg.db_table,
            source_id=cfg.source_id,
            run_id=cfg.run_id,
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
        cluster = replace(cluster, crossing_points=tuple(crossing_points))
        topology, annotated, cluster_obs = _build_topology_for_cluster(
            cluster, observations, origin_lon, origin_lat, cfg,
        )
        topologies.append(topology)
        annotated_clusters.append(annotated)
        all_observations.extend(cluster_obs)
    return topologies, annotated_clusters, all_observations


def _detect_intersections_db(conn: Any, cfg: IntersectConfig) -> IntersectResult:
    """DB-mode intersection detection.

    ZH: DB 模式交叉口检测。
        - Method 1+2: 全局流式逐车处理（单车操作，无需空间窗）
        - Method 3: 空间滑窗内跨车段求交
        - 共享 _cluster_pipeline + _build_intersect_report
    """
    # ── Spatial extent ──────────────────────────────────────────────
    extent = _query_spatial_extent(
        conn,
        table=cfg.db_table,
        source_id=cfg.source_id,
    )
    if extent is None:
        return _empty_intersect_result()

    origin_lon = (extent[0] + extent[2]) / 2.0
    origin_lat = (extent[1] + extent[3]) / 2.0

    live = cfg.live_status

    tracker = PerfTracker("intersect2_db")

    # ── Methods 1 & 2: stream per-vehicle (DB-specific) ─────────────
    if live is not None:
        live.update(stage="Intersect2: 流式 / Stream M1+M2",
                    detail="Method 1+2: Turn events + ortho candidates...")
    t_stage = time.perf_counter()
    with tracker.stage("stream_m1_m2"):
        turn_events, ortho_candidates = _stream_extract_methods_1_2(
            conn, cfg, origin_lon, origin_lat,
            live_status=live, perf=tracker,
        )
    if live is not None:
        live.update(
            detail=f"Turn events: {len(turn_events):,}, Ortho: {len(ortho_candidates):,}",
            elapsed=time.perf_counter() - t_stage,
        )

    # ── Method 3: spatial windows (DB-specific) ─────────────────────
    t_stage = time.perf_counter()
    with tracker.stage("cross_track_windows"):
        cross_candidates = _extract_cross_track_windows(
            conn, cfg, extent, origin_lon, origin_lat, live_status=live, perf=tracker,
        )

    # ── Shared: cluster pipeline (G* + DBSCAN + merge + ablation) ───
    if live is not None:
        live.update(stage="Intersect2: 聚类 / Clustering",
                    detail="G* + DBSCAN + merge + ablation...")
    t_stage = time.perf_counter()
    with tracker.stage("cluster_pipeline"):
        hotspot_events, annotated_clusters = _cluster_pipeline(
            turn_events, ortho_candidates, cross_candidates,
            origin_lon, origin_lat, cfg,
            live_status=live, perf=tracker,
        )
    if live is not None:
        live.update(
            detail=f"Clusters: {len(annotated_clusters):,}",
            elapsed=time.perf_counter() - t_stage,
        )

    # ── DB-mode topology (per-cluster DB queries) ───────────────────
    if live is not None:
        live.update(stage="Intersect2: 拓扑验证 / Topology",
                    detail="Topology verification (per-cluster DB queries)...")
    t_stage = time.perf_counter()
    with tracker.stage("topology_db"):
        topologies, annotated_clusters, observations = _build_topologies_from_db(
            conn, annotated_clusters, cfg, origin_lon, origin_lat,
            live_status=live, perf=tracker,
        )
    if live is not None:
        live.update(
            detail=f"Topologies: {len(topologies):,}",
            elapsed=time.perf_counter() - t_stage,
        )

    log_info(tracker.report())
    return IntersectResult(
        turn_events=turn_events,
        hotspot_events=hotspot_events,
        orthogonal_candidates=ortho_candidates,
        clusters=annotated_clusters,
        topologies=topologies,
        observations=observations,
        cross_track_candidates=cross_candidates,
        report=_build_intersect_report(
            annotated_clusters, topologies,
            turn_events, hotspot_events, ortho_candidates, cross_candidates,
            track_count=0,
        ),
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
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
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
    log_info(f"── 转向事件检测 / Turn event detection ({total_tracks:,} 轨迹/tracks) ...")

    for vehicle_id, track in _perf_iter(perf, tracks.items(), name="tracks_turn"):
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
                        source_ids=(p.point_ref.source_id,),
                        from_source_id=p.point_ref.source_id,
                        from_source_row_key=p.point_ref.source_row_key,
                    )
                )

        track_count += 1
        now = time.perf_counter()
        if track_count % 100 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"已处理/Processed:\t{track_count:,}/{total_tracks:,}\t轨迹/tracks",
                progress=track_count / total_tracks,
                elapsed=now - t0,
                stage="Intersect2: 转向事件 / Turn Events",
            )
            last_t = now

    log_info(f"── 转向事件检测: {len(events):,} 事件/events ({time.perf_counter() - t0:.1f}s)")
    return events


@track_for_loops(
    {
        "enumerate(events)": "gstar_events",
        "events": "gstar_neighbors",
    },
    depth=2,
)
def _compute_getis_ord_g_star(
    events: list[TurnEvent], radius_m: float,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> dict[int, float]:
    """Return {event_id: z_score} for Getis-Ord G* spatial concentration.

    Uses a binary intensity model: each event contributes 1.0 to local
    count if within ``radius_m``. Since TurnEvent is frozen, z-scores
    are returned as a separate dict instead of mutating event fields.

    For-loop timing is auto-injected by ``@track_for_loops`` —
    ``gstar_events`` (depth 1) and ``gstar_neighbors`` (depth 2).
    """
    n = len(events)
    if n < 2:
        return {e.event_id: 0.0 for e in events}

    t0 = time.perf_counter()
    last_t = t0
    log_info(f"    计算 Getis-Ord G* / Computing G* ({n:,} 事件/events) ...")
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
            _route_progress(
                live_status,
                detail=f"G*:\t{i + 1:,}/{n:,}\t事件/events",
                progress=(i + 1) / n,
                elapsed=now - t0,
                stage="Intersect2: G* 热点 / Hotspot G*",
            )
            last_t = now

    log_info(f"    G*: {n:,} 事件/events ({time.perf_counter() - t0:.1f}s)")

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
    events: list[TurnEvent], cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[TurnEvent]:
    """Filter turn events by Getis-Ord G* z-score and minimum turn angle."""
    if not events:
        return []

    log_info(f"── 热点筛选 / Hotspot selection ({len(events):,} 事件/events) ...")
    t0 = time.perf_counter()
    z_scores = _compute_getis_ord_g_star(events, cfg.g_star_radius_m, live_status=live_status, perf=perf)
    result = [
        e
        for e in events
        if z_scores.get(e.event_id, 0.0) >= cfg.g_star_min_z
        and abs(e.turn_deg) >= cfg.min_turn_deg
    ]
    log_info(f"── 热点筛选: {len(result):,} 事件/events ({time.perf_counter() - t0:.1f}s)")
    return result


# ═══════════════════════════════════════════════════════════════════════
# Method 2: Orthogonal intersection candidate extraction
# ═══════════════════════════════════════════════════════════════════════


def _extract_orthogonal_candidates(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[OrthogonalCandidate]:
    """Extract Cleaner 1 orthogonal injection points as candidates.

    These are synthetic TrajectoryPoints with
    ``point_ref.generated_reason == "orthogonal_heading"``. They mark
    locations where two consecutive GPS headings are ~90° apart,
    suggesting a road intersection.
    """
    candidates: list[OrthogonalCandidate] = []
    total_tracks = len(tracks)
    t0 = time.perf_counter()
    last_t = t0
    track_count = 0

    for track in _perf_iter(perf, tracks.values(), name="tracks_ortho", total=total_tracks):
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

        track_count += 1
        now = time.perf_counter()
        if track_count % 100 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"正交候选/Ortho:\t{track_count:,}/{total_tracks:,}\t轨迹/tracks,\t{len(candidates):,}\t候选",
                progress=track_count / total_tracks,
                elapsed=now - t0,
                stage="Intersect2: 正交候选 / Ortho Candidates",
            )
            last_t = now

    log_info(f"── 正交候选 / Ortho candidates: {len(candidates):,} 候选 ({time.perf_counter() - t0:.1f}s)")
    return candidates


# ═══════════════════════════════════════════════════════════════════════
# Method 3: Cross-track orthogonal segment intersection
# ═══════════════════════════════════════════════════════════════════════


def _emit_cross_track_progress(
    live_status: Any,
    pair_count: int,
    total_pairs: int,
    candidate_count: int,
    elapsed: float,
    *,
    sub_detail: bool = False,
    final: bool = False,
) -> str:
    """ZH: 跨车求交进度输出（format string + 输出分支的唯一一处）。

    EN: Cross-track progress emit — single source of truth for format + routing.

    When *sub_detail* is False (standalone/handle mode): writes to the main
    ``detail`` row and updates the progress bar.

    When *sub_detail* is True (inside a spatial window): writes to the
    ``sub_detail`` row without touching the progress bar (owned by the
    window loop).

    *final* drops the "计算中：" prefix for the final summary line.
    """
    detail = (
        f"跨车求交/Cross-track:\t{pair_count:,}/{total_pairs:,}\t车辆对/pairs,\t"
        f"{candidate_count:,}\t候选/candidates"
    )
    line = f"计算中：{detail}" if not final else detail
    if live_status is not None:
        if sub_detail:
            live_status.update(
                sub_detail=line,
                elapsed=elapsed,
            )
        else:
            live_status.update(
                progress=(pair_count / total_pairs) if total_pairs else 0.0,
                detail=line,
                elapsed=elapsed,
            )
    else:
        print(f"    ... {line} ({elapsed:.1f}s)")
    return detail


def _extract_orthogonal_cross_track_candidates(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: IntersectConfig,
    live_status: Any = None,
    *,
    sub_detail: bool = False,
    perf: PerfTracker | None = None,
) -> tuple[list[CrossTrackCandidate], int, int]:
    """Multi-vehicle segment crossing detection via NumPy batched intersection.

    ZH: Method 3 —— 多车轨迹段正交交点检测。
        1. 预过滤：只保留 heading 前后差 ≤ straightness_deg 的"直段"
        2. 按车辆分组段坐标和 heading
        3. 每对不同车辆间一次 NumPy broadcasting 完成所有段对的求交+夹角过滤

    段 S_j = (P_j → P_{j+1}) 的方位角用 ``track[j].heading`` 近似。

    Returns ``(candidates, pair_count, total_pairs)`` so callers can build
    window-done log lines.

    If *sub_detail* is True, progress writes to the LiveStatus ``sub_detail``
    row without touching the progress bar (owned by the enclosing window loop).
    """
    # ── Step 1: 预过滤直段 (heading 前后差 ≤ straightness_deg) ──────
    # track_segs[vid] = [(x1,y1, x2,y2, heading, timestamp, seg_idx), ...]
    track_segs: dict[str, list[tuple[float, float, float, float, float, float, int]]] = {}
    for vid, track in tracks.items():
        if len(track) < 2:
            continue
        segs: list[tuple[float, float, float, float, float, float, int]] = []
        for si in range(len(track) - 1):
            h0 = track[si].heading
            h1 = track[si + 1].heading
            if abs(_angle_delta_deg(h0, h1)) > cfg.cross_track_straightness_deg:
                continue  # 跳过转弯段 / skip turning segments
            xy0 = _to_xy(track[si].lon, track[si].lat, origin_lon, origin_lat)
            xy1 = _to_xy(track[si + 1].lon, track[si + 1].lat, origin_lon, origin_lat)
            segs.append((xy0[0], xy0[1], xy1[0], xy1[1],
                         track[si].heading, track[si].timestamp, si))
        if segs:
            track_segs[vid] = segs

    vehicle_ids = sorted(track_segs)
    if len(vehicle_ids) < 2:
        return [], 0, 0

    # ── Step 2: 每对车辆间 NumPy 批量求交 ──────────────────────────
    candidates: list[CrossTrackCandidate] = []
    total_vehicles = len(vehicle_ids)
    total_pairs = total_vehicles * (total_vehicles - 1) // 2
    pair_count = 0
    t0 = time.perf_counter()
    last_t = t0

    for i in _perf_iter(perf, range(len(vehicle_ids)), name="cross_track_vehicle_i", total=len(vehicle_ids)):
        vid_a = vehicle_ids[i]
        segs_a = track_segs[vid_a]
        a1 = np.array([[s[0], s[1]] for s in segs_a], dtype=float)
        a2 = np.array([[s[2], s[3]] for s in segs_a], dtype=float)
        ha = np.array([s[4] for s in segs_a], dtype=float)
        ts_a = [s[5] for s in segs_a]

        # ZH: 内层 j 循环由 NumPy 批量求交处理，不做逐迭代计时。
        # EN: Inner j loop is handled by the NumPy batch — not timed per-iteration.
        for j in range(i + 1, len(vehicle_ids)):
            vid_b = vehicle_ids[j]
            segs_b = track_segs[vid_b]
            b1 = np.array([[s[0], s[1]] for s in segs_b], dtype=float)
            b2 = np.array([[s[2], s[3]] for s in segs_b], dtype=float)
            hb = np.array([s[4] for s in segs_b], dtype=float)
            ts_b = [s[5] for s in segs_b]

            cx, cy, flat_idx = _batch_segment_intersections(
                a1, a2, ha, b1, b2, hb,
                cfg.cross_track_min_angle_deg,
                cfg.cross_track_max_angle_deg,
            )
            for fi in flat_idx:
                si = fi // len(segs_b)
                sj = fi % len(segs_b)
                a_start = tracks[vid_a][segs_a[si][6]]
                a_end = tracks[vid_a][segs_a[si][6] + 1]
                b_start = tracks[vid_b][segs_b[sj][6]]
                b_end = tracks[vid_b][segs_b[sj][6] + 1]
                lon, lat = _to_lonlat(
                    float(cx.flat[fi]), float(cy.flat[fi]),
                    origin_lon, origin_lat,
                )
                ts = (ts_a[si] + ts_b[sj]) / 2.0
                candidates.append(
                    CrossTrackCandidate(
                        point_ref=a_start.point_ref,
                        vehicle_id=f"{vid_a}|{vid_b}",
                        timestamp=ts,
                        lon=lon,
                        lat=lat,
                        x=float(cx.flat[fi]),
                        y=float(cy.flat[fi]),
                        heading=segs_a[si][4],
                        speed=a_start.speed,
                        from_source_id=a_start.point_ref.source_id,
                        from_source_row_key=(
                            f"{a_start.point_ref.source_row_key},{a_end.point_ref.source_row_key}"
                        ),
                        to_source_id=b_start.point_ref.source_id,
                        to_source_row_key=(
                            f"{b_start.point_ref.source_row_key},{b_end.point_ref.source_row_key}"
                        ),
                        origin_lon=origin_lon,
                        origin_lat=origin_lat,
                    )
                )

            pair_count += 1
            now = time.perf_counter()
            if pair_count % 100 == 0 or (now - last_t) >= 3.0:
                _emit_cross_track_progress(
                    live_status, pair_count, total_pairs,
                    len(candidates), now - t0,
                    sub_detail=sub_detail,
                )
                last_t = now

    elapsed = time.perf_counter() - t0
    _emit_cross_track_progress(
        live_status, pair_count, total_pairs, len(candidates), elapsed,
        sub_detail=sub_detail, final=True,
    )
    return candidates, pair_count, total_pairs


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
    live_status: Any = None,
    stage: str = "DBSCAN",
    perf: PerfTracker | None = None,
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

    for i in _perf_iter(perf, range(n), name="dbscan_points", total=n):
        if labels[i] != _UNVISITED:
            continue
        neighbors = _neighbors_indices(xs, ys, i, eps_m)
        if len(neighbors) < min_samples:
            labels[i] = _NOISE
            continue

        labels[i] = cluster_id
        seeds = [nb for nb in neighbors if nb != i]
        # ZH: while 循环不是可迭代对象，跳过逐迭代计时（仅 seed 展开阶段的
        #     内层 for 循环被计时）。
        # EN: ``while`` is not an iterable — no per-iteration timing here;
        #     only the inner seed-expansion for loop is timed.
        while seeds:
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_id
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_id
            cur_neighbors = _neighbors_indices(xs, ys, current, eps_m)
            if len(cur_neighbors) >= min_samples:
                for nb in _perf_iter(perf, cur_neighbors, name="dbscan_seed_neighbors"):
                    if labels[nb] in (_UNVISITED, _NOISE) and nb not in seeds:
                        seeds.append(nb)
        cluster_id += 1
        now = time.perf_counter()
        if i % 500 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"DBSCAN:\t{i + 1:,}/{n:,}\t点/points,\t{cluster_id}\t簇/clusters",
                progress=(i + 1) / n,
                elapsed=now - t0,
                stage=f"Intersect2: DBSCAN ({stage})",
            )
            last_t = now

    log_info(f"    DBSCAN: {n:,} 点/points → {cluster_id:,} 簇/clusters ({time.perf_counter() - t0:.1f}s)")
    return labels


def _cluster_dbscan(
    events: list[TurnEvent],
    origin_lon: float,
    origin_lat: float,
    *,
    eps_m: float,
    min_samples: int,
    source: str = "turn",
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[HotspotCluster]:
    """DBSCAN-cluster TurnEvents and build HotspotClusters."""
    n = len(events)
    if n == 0:
        return []

    xs = [e.x for e in events]
    ys = [e.y for e in events]
    log_info(f"    DBSCAN 聚类 ({source}) / DBSCAN clustering ({n:,} 点/points) ...")
    labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples,
                          live_status=live_status, stage=source, perf=perf)

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
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[HotspotCluster]:
    """DBSCAN-cluster OrthogonalCandidates, wrapping them as TurnEvents."""
    n = len(candidates)
    if n == 0:
        return []

    xs = [c.x for c in candidates]
    ys = [c.y for c in candidates]
    log_info(f"    DBSCAN 聚类 (orthogonal) / DBSCAN clustering ({n:,} 点/points) ...")
    labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples,
                          live_status=live_status, stage="orthogonal", perf=perf)

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
                source_ids=(c.point_ref.source_id,),
                from_source_id=c.point_ref.source_id,
                from_source_row_key=c.point_ref.from_ref,
                to_source_id=c.point_ref.source_id,
                to_source_row_key=c.point_ref.to_ref,
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


def _cluster_orthogonal_cross_track_candidates(
    candidates: list[CrossTrackCandidate],
    origin_lon: float,
    origin_lat: float,
    *,
    eps_m: float,
    min_samples: int,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[HotspotCluster]:
    """DBSCAN-cluster orthogonal_cross_track candidates, wrapping as TurnEvents.

    ZH: 将多车正交候选点包装为 ``TurnEvent(source="orthogonal_cross_track")``
        后 DBSCAN 聚类。结构与 ``_cluster_orthogonal_candidates`` 一致。
    """
    n = len(candidates)
    if n == 0:
        return []

    xs = [c.x for c in candidates]
    ys = [c.y for c in candidates]
    log_info(f"    DBSCAN 聚类 (orthogonal_cross_track) / DBSCAN clustering ({n:,} 点/points) ...")
    labels = _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples,
                          live_status=live_status, stage="orthogonal_cross_track", perf=perf)

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
                source="orthogonal_cross_track",
                point_ref=c.point_ref,
                source_ids=tuple(sorted({c.from_source_id, c.to_source_id})),
                from_source_id=c.from_source_id,
                from_source_row_key=c.from_source_row_key,
                to_source_id=c.to_source_id,
                to_source_row_key=c.to_source_row_key,
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
            cluster_ref=f"cluster_orthogonal_cross_track_{cid}",
            events=cluster_events,
            source="orthogonal_cross_track",
        )
        clusters.append(
            _with_source_provenance(
                cluster,
                source_counts={"orthogonal_cross_track": len(cluster_items)},
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
    *,
    perf: PerfTracker | None = None,
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

    # ZH: ``while remaining`` 不是可迭代对象，跳过逐迭代计时；仅内部扫描循环
    #     用 ``merge_scan`` 计时（每个外层 while 迭代各计时一轮）。
    # EN: ``while`` is not an iterable — the outer absorb loop is not timed;
    #     the inner scan ``for cj in remaining`` is timed as ``merge_scan``.
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

            for cj in _perf_iter(perf, remaining, name="merge_scan"):
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
    entry_crossing_id: int | None = None,
    exit_crossing_id: int | None = None,
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
        entry_crossing_id=entry_crossing_id,
        exit_crossing_id=exit_crossing_id,
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
        _route_progress(
            live_status,
            detail=f"簇/Cluster:\t{cluster_idx:,}/{total_clusters:,}",
            progress=cluster_idx / total_clusters,
            elapsed=time.perf_counter() - t0,
            stage="Intersect2: 拓扑验证 / Topology",
        )
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

    log_info(f"── 拓扑验证: {len(topologies):,} 拓扑/topologies ({time.perf_counter() - t0:.1f}s)")
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
    crossing_id_by_segment: dict[tuple[str, int, bool], int] = {}
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
                end_point_ref=track_lookup[vehicle_id][seg_idx + 1].point_ref,
            )
        )
        crossing_id_by_segment[(vehicle_id, seg_idx, is_entry)] = crossing_counter
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
            entry_crossing_id=crossing_id_by_segment.get((vehicle_id, entry_seg, True)),
            exit_crossing_id=crossing_id_by_segment.get((vehicle_id, exit_seg, False)),
        )
        if obs is not None:
            observations.append(obs)
    return observations, crossing_points
