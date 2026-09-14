"""Detect — Intersection candidate detection and topology extraction.

detect_intersections(source, *, config=None, spatial_context=...) → IntersectResult

Two complementary methods feed into the same topology verification:
  Method 1 (turn events): Bearing-change detection → Getis-Ord G* → DBSCAN
  Method 3 (cross-track): Cross-track intersections → DBSCAN

Both produce HotspotCluster candidates that are merged, deduplicated, and
verified via entry/exit circle-crossing analysis + branch-angle clustering.
"""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, TYPE_CHECKING

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
)
from .exceptions import V2Error
from .cross_track import CrossTrackCapacityError, extract_cross_track_candidates

from .models import (
    CrossTrackCandidate,
    CrossingPoint,
    EntryExitObservation,
    HotspotCluster,
    InOutIntersectionPoint,
    IntersectReport,
    IntersectionTopology,
    PointRef,
    SpatialContext,
    TrackModel,
    TrajectoryPoint,
    TurnEvent,
)
from .projection import CoordinateTransformer
from .db.queries import (
    SpatialWindow,
    SpatialWindowIterator,
    _query_active_points_in_window,
    _query_spatial_extent,
    _stream_vehicle_tracks_from_db,
    _tracks_from_points,
    fetch_refine_run_row,
)
from .db.source_table import verify_refine_source_fingerprint
from .parser import BaseTrajectorySourceHandle as TrajectorySourceHandle


_DB_GLOBAL_GRID_MEMBERSHIP_LIMIT = 8_000_000
_DB_GLOBAL_OVERFLOW_PAIR_LIMIT = 20_000_000


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
    """Detect configuration for turn and cross-track detection.

    PRD v2 Detect §7
    """

    # ── Mode toggles ───────────────────────────────────────────────
    use_turn_events: bool = True
    use_cross_track_candidates: bool = True

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

    # ── Cross-track orthogonal detection (Method 3) ──────────────────
    cross_track_straightness_deg: float = 15.0
    cross_track_min_angle_deg: float = 80.0
    cross_track_max_angle_deg: float = 100.0
    cross_track_dbscan_eps_m: float = 100.0
    cross_track_dbscan_min_samples: int = 3
    # Structural memory bounds owned by cross_track.py's implementation.
    cross_track_grid_cell_m: float = 100.0
    cross_track_pair_batch_size: int = 65_536
    cross_track_max_grid_cells_per_segment: int = 25

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
    kept_table: str | None = None
    source_table: str | None = None
    refine_id: str | None = None
    spatial_window_size: tuple[float, float] = (2000.0, 2000.0)
    spatial_window_overlap: float = 0.15
    stream_batch_size: int = 100

    # ── Live status display (optional, set by CLI for dynamic terminal output) ──
    live_status: Any = None


# ── Result ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntersectResult(TrackModel):
    """Output of Detect stage.

    All fields populated eagerly during detect_intersections().
    """

    turn_events: list[TurnEvent]
    hotspot_events: list[TurnEvent]
    clusters: list[HotspotCluster]
    topologies: list[IntersectionTopology]
    report: IntersectReport
    spatial_context: SpatialContext
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


def _extend_ref_lists(
    merged: dict[str, list[PointRef]],
    incoming: dict[str, tuple[PointRef, ...]],
) -> None:
    """Append provenance in place; tuple conversion is deferred to output."""
    for source, items in incoming.items():
        merged.setdefault(source, []).extend(items)


def _build_candidate_clusters(
    hotspot_events: list[TurnEvent],
    cross_track_candidates: list[CrossTrackCandidate],
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
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
                transformer,
                eps_m=cfg.dbscan_eps_m_turn,
                min_samples=cfg.dbscan_min_samples_turn,
                source="turn",
                perf=perf,
            )
        )
    if "orthogonal_cross_track" not in disabled and cross_track_candidates:
        clusters.extend(
            _cluster_orthogonal_cross_track_candidates(
                cross_track_candidates,
                transformer,
                eps_m=cfg.cross_track_dbscan_eps_m,
                min_samples=cfg.cross_track_dbscan_min_samples,
                perf=perf,
            )
        )
    return _merge_clusters(
        clusters, cfg.merge_centroid_distance_m, transformer, perf=perf
    )


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
    cross_track_candidates: list[CrossTrackCandidate],
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
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
            cross_track_candidates,
            cfg,
            transformer,
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
                    stage="Detect: 源消融 / Source Ablation",
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
    complete_indexes = {
        topology.cluster_calculate_index
        for topology in topologies
        if topology.is_complete
    }
    complete_clusters = [
        cluster
        for cluster in clusters
        if cluster.cluster_calculate_index in complete_indexes
    ]

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


def _point_xy(point: TrajectoryPoint) -> tuple[float, float]:
    """Return one point's Run-scoped processing coordinates."""
    if point.x is None or point.y is None:
        raise V2Error(
            "trajectory point is missing processing-CRS x/y; project the track "
            "with the Run SpatialContext before running Detect"
        )
    return float(point.x), float(point.y)


def _project_tracks(
    tracks: dict[str, list[TrajectoryPoint]],
    transformer: CoordinateTransformer,
    *,
    trust_existing: bool = False,
) -> dict[str, list[TrajectoryPoint]]:
    """Attach Run-scoped x/y, trusting only coordinates produced by our DB query.

    A generic in-memory handle may carry legacy local-origin x/y with no CRS
    metadata, so those values are deliberately recomputed from 4326 truth.
    """
    projected_tracks: dict[str, list[TrajectoryPoint]] = {}
    for vehicle_id, track in tracks.items():
        pending: list[TrajectoryPoint] = []
        for point in track:
            if (point.x is None) != (point.y is None):
                raise V2Error("trajectory point must provide both processing x and y")
            if not trust_existing or point.x is None:
                pending.append(point)
        generated = iter(
            transformer.forward_many((point.lon, point.lat) for point in pending)
            if pending
            else ()
        )
        projected: list[TrajectoryPoint] = []
        for point in track:
            if not trust_existing or point.x is None:
                x, y = next(generated)
                projected.append(replace(point, x=x, y=y))
            else:
                projected.append(point)
        projected_tracks[vehicle_id] = projected
    return projected_tracks


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


def _empty_intersect_result(spatial_context: SpatialContext) -> IntersectResult:
    """Common empty result for both modes when no data is available."""
    return IntersectResult(
        turn_events=[], hotspot_events=[],
        clusters=[], topologies=[],
        report=IntersectReport(
            track_count=0, turn_event_count=0, hotspot_event_count=0,
            cross_track_candidate_count=0,
            cluster_count=0, cluster_count_turn=0,
            cluster_count_cross_track=0, complete_intersection_count=0,
        ),
        spatial_context=spatial_context,
        cross_track_candidates=[],
    )


def _build_intersect_report(
    annotated_clusters: list[HotspotCluster],
    topologies: list[IntersectionTopology],
    turn_events: list[TurnEvent],
    hotspot_events: list[TurnEvent],
    cross_candidates: list[CrossTrackCandidate],
    track_count: int,
) -> IntersectReport:
    """Shared report construction for both modes."""
    turn_clusters = sum(1 for c in annotated_clusters if "turn" in _cluster_sources(c))
    cross_clusters = sum(1 for c in annotated_clusters if "orthogonal_cross_track" in _cluster_sources(c))
    source_report = _complete_source_report(annotated_clusters, topologies)
    return IntersectReport(
        track_count=track_count,
        turn_event_count=len(turn_events),
        hotspot_event_count=len(hotspot_events),
        cross_track_candidate_count=len(cross_candidates),
        cluster_count=len(annotated_clusters),
        cluster_count_turn=turn_clusters,
        cluster_count_cross_track=cross_clusters,
        complete_intersection_count=sum(1 for t in topologies if t.is_complete),
        complete_intersection_source_counts=source_report["source_counts"],
        complete_intersection_source_ratios=source_report["source_ratios"],
        complete_intersection_critical_source_counts=source_report["critical_source_counts"],
        complete_intersection_critical_source_ratios=source_report["critical_source_ratios"],
    )


def _cluster_pipeline(
    turn_events: list[TurnEvent],
    cross_candidates: list[CrossTrackCandidate],
    transformer: CoordinateTransformer,
    cfg: IntersectConfig,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[TurnEvent], list[HotspotCluster]]:
    """Shared G* + DBSCAN, merge, reindex, and ablation for both modes."""
    all_clusters: list[HotspotCluster] = []
    hotspot_events: list[TurnEvent] = []

    # ── Method 1: Turn event → G* → DBSCAN ────────────────────────
    if cfg.use_turn_events and turn_events:
        hotspot_events = _select_hotspot_events(turn_events, cfg, live_status=live_status, perf=perf)
        if hotspot_events:
            all_clusters.extend(_cluster_dbscan(
                hotspot_events, transformer,
                eps_m=cfg.dbscan_eps_m_turn,
                min_samples=cfg.dbscan_min_samples_turn,
                source="turn",
                live_status=live_status,
                perf=perf,
            ))

    # Refine-injected points participate in Method 1 through the ordered
    # trajectory only; they are never promoted directly into DBSCAN here.

    # ── Method 3: Cross-track → DBSCAN ─────────────────────────────
    if cfg.use_cross_track_candidates and cross_candidates:
        all_clusters.extend(_cluster_orthogonal_cross_track_candidates(
            cross_candidates, transformer,
            eps_m=cfg.cross_track_dbscan_eps_m,
            min_samples=cfg.cross_track_dbscan_min_samples,
            live_status=live_status,
            perf=perf,
        ))

    # ── Merge + reindex + ablation ─────────────────────────────────
    merged = _merge_clusters(
        all_clusters, cfg.merge_centroid_distance_m, transformer, perf=perf
    )
    merged = [
        replace(cluster, cluster_calculate_index=index)
        for index, cluster in enumerate(merged)
    ]
    annotated = _annotate_source_ablation(
        merged, hotspot_events, cross_candidates,
        cfg, transformer,
        live_status=live_status,
        perf=perf,
    )
    return hotspot_events, annotated


def _extract_methods_1_3(
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[TurnEvent], list[CrossTrackCandidate]]:
    """Run turn and cross-track extraction on a tracks dict (handle mode)."""
    turn_events = _detect_turn_events(
        tracks, cfg, live_status=live_status, perf=perf
    ) if cfg.use_turn_events else []
    cross_candidates: list[CrossTrackCandidate] = []
    if cfg.use_cross_track_candidates:
        cross_candidates, _, _ = extract_cross_track_candidates(
            tracks, transformer, cfg, live_status=live_status, perf=perf
        )
    return turn_events, cross_candidates


# ── Public entry point ─────────────────────────────────────────────────


def detect_intersections(
    source: TrajectorySourceHandle | None = None,
    *,
    config: IntersectConfig | None = None,
    conn: Any = None,
    spatial_context: SpatialContext | None = None,
) -> IntersectResult:
    """Run intersection detection and topology extraction.

    PRD v2 Detect §2

    Two modes:
      - **Handle mode**: ``source`` is a ``TrajectorySourceHandle``.
        All points are read into memory and grouped by vehicle_id.
      - **DB mode**: ``conn`` is a psycopg connection and
        ``config.query_active_from_db`` is True.  Tracks are loaded once and
        shared by candidate extraction and topology verification; Method 3
        uses a capacity-guarded global spatial index.
    """
    cfg = config or IntersectConfig()
    if conn is not None and cfg.query_active_from_db:
        if cfg.refine_id is None:
            raise V2Error("formal DB Detect requires refine_id")
        refine = fetch_refine_run_row(conn, cfg.refine_id)
        if refine.status != "completed" or not refine.has_source_fingerprint:
            raise V2Error(
                f"Refine Run {cfg.refine_id} is not a completed immutable-source run"
            )
        if spatial_context is not None and spatial_context != refine.spatial_context:
            raise V2Error("supplied SpatialContext does not match the parent Refine Run")
        if cfg.source_table is not None and cfg.source_table != refine.source_table:
            raise V2Error("configured source_table does not belong to the parent Refine Run")
        if cfg.db_table != "trajectory_points" and cfg.db_table != refine.source_table:
            raise V2Error("configured db_table does not belong to the parent Refine Run")
        if cfg.kept_table not in (None, "kept_trajectory_points"):
            raise V2Error("formal DB Detect must use kept_trajectory_points")
        cfg = replace(
            cfg,
            db_table=refine.source_table,
            source_table=refine.source_table,
            kept_table="kept_trajectory_points",
        )
        verify_refine_source_fingerprint(conn, refine.refine_id)
        return _detect_intersections_db(conn, cfg, refine.spatial_context)
    elif source is not None:
        if spatial_context is None:
            raise V2Error(
                "handle Detect requires an explicit SpatialContext; a first-point "
                "or extent-centre projection is not a safe fallback"
            )
        return _detect_intersections_handle(source, cfg, spatial_context)
    else:
        raise V2Error(
            "detect_intersections: provide either 'source' (handle mode) "
            "or 'conn' + query_active_from_db (DB mode)"
        )


def _detect_intersections_handle(
    source: TrajectorySourceHandle,
    cfg: IntersectConfig,
    spatial_context: SpatialContext,
) -> IntersectResult:
    source_context = getattr(source, "spatial_context", None)
    if source_context is not None and source_context != spatial_context:
        raise V2Error(
            "trajectory source SpatialContext does not match Detect SpatialContext"
        )
    tracks = _group_by_vehicle(source)
    if not tracks:
        return _empty_intersect_result(spatial_context)
    transformer = CoordinateTransformer(spatial_context)
    tracks = _project_tracks(tracks, transformer)

    live = cfg.live_status
    own_live = False
    if live is None:
        # ZH: Handle 模式下如果没有外部 LiveStatus，自建一个。
        # EN: If no external LiveStatus in handle mode, create our own.
        try:
            from .logger import LiveStatus
            live = LiveStatus("Detect: 交叉口检测 / Intersection Detection")
            live.start()
            own_live = True
        except Exception:
            live = None
    tracker = PerfTracker("detect_handle")
    try:
        with tracker.stage("extract_methods_1_3"):
            turn_events, cross_candidates = _extract_methods_1_3(
                tracks, cfg, transformer,
                live_status=live, perf=tracker,
            )

        with tracker.stage("cluster_pipeline"):
            hotspot_events, annotated_clusters = _cluster_pipeline(
                turn_events, cross_candidates,
                transformer, cfg,
                live_status=live, perf=tracker,
            )

        with tracker.stage("topology"):
            topologies, annotated_clusters, observations = _build_intersection_topologies(
                annotated_clusters, tracks, transformer, cfg,
                live_status=live, perf=tracker,
            )

        log_info(tracker.report())
        return IntersectResult(
            turn_events=turn_events,
            hotspot_events=hotspot_events,
            clusters=annotated_clusters,
            topologies=topologies,
            observations=observations,
            cross_track_candidates=cross_candidates,
            report=_build_intersect_report(
                annotated_clusters, topologies,
                turn_events, hotspot_events, cross_candidates,
                track_count=len(tracks),
            ),
            spatial_context=spatial_context,
        )
    finally:
        if own_live and live is not None:
            live.stop()


# ═══════════════════════════════════════════════════════════════════════
# DB-mode helper functions
# ═══════════════════════════════════════════════════════════════════════


def _stream_extract_method_1(
    conn: Any, cfg: IntersectConfig,
    spatial_context: SpatialContext,
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[
    list[TurnEvent],
    dict[str, list[TrajectoryPoint]],
]:
    """Load each DB track once and batch Method 1 calculations.

    ZH: 外层 ``for track in _stream_vehicle_tracks_from_db(...)`` 是生成器流，
        逐迭代计时噪声大，整体由调用方 ``tracker.stage("stream_m1")`` 计时。
        返回的 ``tracks`` workspace 会由 Method 3 和拓扑验证复用，避免各阶段
        重新扫描数据库。
    EN: The outer per-track generator stream is timed as a whole by the caller's
        ``tracker.stage("stream_m1")`` — per-iteration timing would be noisy.
        The returned workspace is reused by Method 3 and topology verification.
    """
    turn_events: list[TurnEvent] = []
    t_total = time.perf_counter()
    last_progress_t = t_total
    track_count = 0
    batch: dict[str, list[TrajectoryPoint]] = {}
    tracks: dict[str, list[TrajectoryPoint]] = {}
    read_bounds: tuple[float, float, float, float] | None = None

    log_info("── 流式处理 / Streaming tracks and turn events ...")
    for track in _stream_vehicle_tracks_from_db(
        conn,
        table=cfg.db_table,
        kept_table=cfg.kept_table,
        source_table=cfg.source_table,
        refine_id=cfg.refine_id,
        spatial_context=spatial_context,
    ):
        for point in track:
            if not (math.isfinite(point.lon) and math.isfinite(point.lat)):
                raise V2Error("DB point has non-finite longitude/latitude")
            if (point.x is None) != (point.y is None):
                raise V2Error("DB point has incomplete processing x/y")
            if point.x is not None and not (
                math.isfinite(point.x) and math.isfinite(point.y)  # type: ignore[arg-type]
            ):
                raise V2Error("DB point has non-finite processing x/y")
            if not (-180.0 <= point.lon <= 180.0 and -90.0 <= point.lat <= 90.0):
                raise V2Error("DB point longitude/latitude is outside EPSG:4326")
            point_bounds = (point.lon, point.lat, point.lon, point.lat)
            read_bounds = (
                point_bounds
                if read_bounds is None
                else (
                    min(read_bounds[0], point.lon),
                    min(read_bounds[1], point.lat),
                    max(read_bounds[2], point.lon),
                    max(read_bounds[3], point.lat),
                )
            )
        subtrack_index = (
            track[0].refined_point_ref.subtrack_index
            if track[0].refined_point_ref is not None
            else 1
        )
        track_key = f"{track[0].vehicle_id}\x00{subtrack_index}"
        track = _project_tracks(
            {track_key: track}, transformer, trust_existing=True
        )[track_key]
        if any(
            point.x is None or point.y is None
            or not math.isfinite(point.x) or not math.isfinite(point.y)
            for point in track
        ):
            raise V2Error("DB point could not provide finite processing x/y")
        tracks[track_key] = track
        batch[track_key] = track
        track_count += 1

        if len(batch) >= cfg.stream_batch_size:
            # ── Flush batch ──
            if cfg.use_turn_events:
                for te in _detect_turn_events(batch, cfg, perf=perf):
                    turn_events.append(replace(
                        te, event_calculate_index=len(turn_events),
                    ))
            batch.clear()

            now = time.perf_counter()
            if (now - last_progress_t) >= 3.0:
                if live_status is not None:
                    live_status.update(
                        detail=f"流式/Streaming:\t{track_count:,}\t轨迹/tracks,\t"
                               f"{len(turn_events):,}\t转向/turn",
                        elapsed=now - t_total,
                        stage="Detect: 流式 / Stream M1",
                    )
                last_progress_t = now

    # ── Flush remaining ──
    if batch:
        if cfg.use_turn_events:
            for te in _detect_turn_events(batch, cfg, perf=perf):
                turn_events.append(replace(
                    te, event_calculate_index=len(turn_events),
                ))
    if read_bounds is not None:
        transformer.validate_bounds(read_bounds)
    log_info(f"── 流式处理: {track_count:,} 轨迹/tracks → "
             f"{len(turn_events):,} 转向事件/turn events "
             f"({time.perf_counter() - t_total:.1f}s)")
    return turn_events, tracks


def _db_global_grid_cell_cap(cfg: IntersectConfig) -> int:
    """Choose a bounded cap that covers segments fitting the former DB window."""
    if cfg.cross_track_grid_cell_m <= 0.0:
        return cfg.cross_track_max_grid_cells_per_segment
    padded_width = cfg.spatial_window_size[0] * (
        1.0 + 2.0 * max(0.0, cfg.spatial_window_overlap)
    )
    padded_height = cfg.spatial_window_size[1] * (
        1.0 + 2.0 * max(0.0, cfg.spatial_window_overlap)
    )
    cells_x = math.ceil(padded_width / cfg.cross_track_grid_cell_m) + 1
    cells_y = math.ceil(padded_height / cfg.cross_track_grid_cell_m) + 1
    return max(cfg.cross_track_max_grid_cells_per_segment, cells_x * cells_y)


def _point_window_indexes(
    value: float,
    *,
    extent_min: float,
    step: float,
    count: int,
    overlap: float,
) -> range:
    """Return a small conservative index range; callers apply exact bounds."""
    if count <= 0 or not math.isfinite(value) or step <= 0.0:
        return range(0)
    base = min(count - 1, max(0, math.floor((value - extent_min) / step)))
    radius = math.ceil(max(0.0, overlap)) + 1
    return range(max(0, base - radius), min(count, base + radius + 1))


def _extract_cross_track_memory_windows(
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    extent: tuple[float, float, float, float],
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[CrossTrackCandidate]:
    """Run the former window semantics from the shared in-memory workspace.

    Points are routed to every exact overlapping window once.  Only populated
    windows are then processed, so this fallback performs no per-window SQL and
    retains the historical clipping/deduplication behaviour.
    """
    if not cfg.use_cross_track_candidates:
        return []
    iterator = SpatialWindowIterator(
        extent,
        size=cfg.spatial_window_size,
        overlap=cfg.spatial_window_overlap,
    )
    windows = list(iterator.iter_windows())
    if not windows:
        return []
    x_count, y_count = iterator.window_shape()
    buckets: dict[int, dict[str, list[TrajectoryPoint]]] = {}
    assigned_points = 0
    for vehicle_id, track in tracks.items():
        for point in track:
            point_x, point_y = _point_xy(point)
            x_indexes = _point_window_indexes(
                point_x,
                extent_min=extent[0],
                step=iterator._step_x,
                count=x_count,
                overlap=cfg.spatial_window_overlap,
            )
            y_indexes = _point_window_indexes(
                point_y,
                extent_min=extent[1],
                step=iterator._step_y,
                count=y_count,
                overlap=cfg.spatial_window_overlap,
            )
            for x_index in x_indexes:
                for y_index in y_indexes:
                    window_index = x_index * y_count + y_index
                    window = windows[window_index]
                    if (
                        window.x_min <= point_x <= window.x_max
                        and window.y_min <= point_y <= window.y_max
                    ):
                        buckets.setdefault(window_index, {}).setdefault(
                            vehicle_id, []
                        ).append(point)
                        assigned_points += 1

    candidates: list[CrossTrackCandidate] = []
    seen_segment_pairs: set[tuple[object, ...]] = set()
    populated = sorted(buckets)
    for processed, window_index in enumerate(
        _perf_iter(
            perf,
            populated,
            name="spatial_windows_memory",
            total=len(populated),
        ),
        start=1,
    ):
        if live_status is not None:
            live_status.update(
                progress=processed / len(populated),
                detail=(
                    "内存窗口/In-memory windows:\t"
                    f"{processed:,}/{len(populated):,} populated"
                ),
                stage="Detect: 内存分窗 / In-memory Windows",
            )
        window_candidates, _, _ = extract_cross_track_candidates(
            buckets[window_index],
            transformer,
            cfg,
            live_status=live_status,
            sub_detail=True,
            perf=perf,
        )
        for candidate in window_candidates:
            key = _cross_track_candidate_key(candidate)
            if key not in seen_segment_pairs:
                seen_segment_pairs.add(key)
                candidates.append(replace(
                    candidate,
                    cross_track_calculate_index=len(candidates),
                ))

    log_info(
        "    内存分窗 / In-memory windows: "
        f"{len(populated):,}/{len(windows):,} 非空/populated, "
        f"{assigned_points:,} 点引用/point refs → "
        f"{len(candidates):,} 候选/candidates"
    )
    return candidates


def _extract_cross_track_db_workspace(
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    extent: tuple[float, float, float, float],
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[CrossTrackCandidate]:
    """Prefer one global broad phase, with a no-SQL memory-window fallback."""
    if not cfg.use_cross_track_candidates:
        return []
    global_cfg = replace(
        cfg,
        cross_track_max_grid_cells_per_segment=_db_global_grid_cell_cap(cfg),
    )
    telemetry: dict[str, int | float] = {}
    fallback_reason: str | None = None
    try:
        candidates, _, _ = extract_cross_track_candidates(
            tracks,
            transformer,
            global_cfg,
            live_status=live_status,
            perf=perf,
            telemetry=telemetry,
            max_grid_memberships=_DB_GLOBAL_GRID_MEMBERSHIP_LIMIT,
            max_overflow_pair_space=_DB_GLOBAL_OVERFLOW_PAIR_LIMIT,
        )
    except CrossTrackCapacityError as exc:
        fallback_reason = str(exc)

    if fallback_reason is not None:
        log_info(
            "    全局空间索引超出预算，切换内存分窗 / "
            "Global index over budget; using memory windows: "
            f"{fallback_reason}"
        )
        return _extract_cross_track_memory_windows(
            tracks,
            cfg,
            extent,
            transformer,
            live_status=live_status,
            perf=perf,
        )

    log_info(
        "    全局空间索引 / Global spatial index: "
        f"cap={global_cfg.cross_track_max_grid_cells_per_segment:,}, "
        f"memberships={int(telemetry.get('grid_memberships_estimate', 0)):,}, "
        f"overflow={int(telemetry.get('overflow_segments', 0)):,}, "
        f"{len(candidates):,} 候选/candidates"
    )
    return candidates


def _extract_cross_track_windows(
    conn: Any, cfg: IntersectConfig,
    extent: tuple[float, float, float, float],
    spatial_context: SpatialContext,
    transformer: CoordinateTransformer,
    live_status: Any = None,
    candidate_extractor: Callable[..., tuple[list[CrossTrackCandidate], int, int]] | None = None,
    legacy_origin: tuple[float, float] | None = None,
    windows_override: list[SpatialWindow] | None = None,
    perf: PerfTracker | None = None,
    telemetry: dict[str, int | float] | None = None,
) -> list[CrossTrackCandidate]:
    """Method 3: spatial sliding windows for cross-track segment intersection.

    Keyword-only benchmark seams never alter production results:
    ``candidate_extractor`` selects the legacy oracle, whose local origin must
    be supplied explicitly via ``legacy_origin``. ``windows_override`` pins the
    window set; ``telemetry`` accumulates per-step timing/counts for the manual
    benchmark. ``perf`` feeds the hierarchical stage tracker.
    """
    candidates: list[CrossTrackCandidate] = []
    seen_segment_pairs: set[tuple[object, ...]] = set()
    if not cfg.use_cross_track_candidates:
        return candidates
    extractor = candidate_extractor or extract_cross_track_candidates

    if windows_override is None:
        windows = SpatialWindowIterator(
            extent,
            size=cfg.spatial_window_size,
            overlap=cfg.spatial_window_overlap,
        )
        window_iterator = windows.iter_windows()
        total_windows = windows.count_windows()
    else:
        window_iterator = iter(windows_override)
        total_windows = len(windows_override)
    t0 = time.perf_counter()
    window_count = 0
    total_points = 0
    last_progress_t = t0
    last_cross_sub = ""
    log_info(f"── 空间滑窗 / Spatial windows (Method 3: orthogonal_cross_track) ──")

    for window in _perf_iter(perf,
        window_iterator,
        name="spatial_windows", 
        total=total_windows):
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
                stage="Detect: 空间滑窗 / Spatial Windows",
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
            kept_table=cfg.kept_table,
            source_table=cfg.source_table,
            refine_id=cfg.refine_id,
            spatial_context=spatial_context,
            telemetry=telemetry,
            _transformer=transformer,
        )
        if telemetry is not None:
            telemetry["windows"] = telemetry.get("windows", 0) + 1
        if not points:
            if telemetry is not None:
                telemetry["empty_windows"] = telemetry.get("empty_windows", 0) + 1
            # log_info(f"Window {window_count} Done, 累计处理点数/sum points: {total_points:,}")
            continue

        total_points += len(points)
        group_started = time.perf_counter() if telemetry is not None else 0.0
        window_tracks = _tracks_from_points(points)
        window_tracks = _project_tracks(
            window_tracks, transformer, trust_existing=True
        )
        if telemetry is not None:
            telemetry["group_sort_seconds"] = telemetry.get(
                "group_sort_seconds", 0.0
            ) + (time.perf_counter() - group_started)
        extract_started = time.perf_counter() if telemetry is not None else 0.0
        extractor_kwargs: dict[str, Any] = {
            "live_status": live_status,
            "sub_detail": True,
        }
        extractor_kwargs.update(perf=perf, telemetry=telemetry)
        if legacy_origin is None:
            win_cands, w_pairs, w_total = extractor(
                window_tracks, transformer, cfg, **extractor_kwargs,
            )
        else:
            # Explicit benchmark seam for the legacy local-origin oracle only.
            win_cands, w_pairs, w_total = extractor(
                window_tracks, legacy_origin[0], legacy_origin[1], cfg,
                **extractor_kwargs,
            )
        if telemetry is not None:
            telemetry["candidate_extraction_seconds"] = telemetry.get(
                "candidate_extraction_seconds", 0.0
            ) + (time.perf_counter() - extract_started)
        added = 0
        dedupe_started = time.perf_counter() if telemetry is not None else 0.0
        for candidate in win_cands:
            key = _cross_track_candidate_key(candidate)
            if key not in seen_segment_pairs:
                seen_segment_pairs.add(key)
                candidates.append(replace(
                    candidate,
                    cross_track_calculate_index=len(candidates),
                ))
                added += 1
        if telemetry is not None:
            telemetry["dedupe_seconds"] = telemetry.get("dedupe_seconds", 0.0) + (
                time.perf_counter() - dedupe_started
            )
            telemetry["windows_with_points"] = telemetry.get("windows_with_points", 0) + 1
            telemetry["window_vehicle_pairs"] = telemetry.get("window_vehicle_pairs", 0) + w_pairs
            telemetry["window_vehicle_pairs_total"] = telemetry.get(
                "window_vehicle_pairs_total", 0
            ) + w_total
            telemetry["dedupe_added"] = telemetry.get("dedupe_added", 0) + added
            telemetry["dedupe_removed"] = telemetry.get("dedupe_removed", 0) + (
                len(win_cands) - added
            )

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
) -> tuple[object, ...]:
    """Stable identity for one canonical A/B segment-pair intersection."""
    if (
        candidate.from_segment_start is not None
        and candidate.to_segment_start is not None
    ):
        return (
            candidate.from_segment_start.point_kind,
            candidate.from_segment_start.point_id,
            candidate.to_segment_start.point_kind,
            candidate.to_segment_start.point_id,
        )
    return (
        candidate.from_segment_start_point_ref.source_id,
        candidate.from_segment_start_point_ref.source_row_key,
        candidate.to_segment_start_point_ref.source_id,
        candidate.to_segment_start_point_ref.source_row_key,
    )


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


def _build_topologies_from_tracks(
    clusters: list[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    transformer: CoordinateTransformer,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> tuple[list[IntersectionTopology], list[HotspotCluster], list[EntryExitObservation]]:
    """Verify DB-mode topologies from the already-streamed global tracks.

    The old architecture issued and sorted one spatial SQL query per cluster.
    This path routes the single ordered stream through all equivalent query
    boxes once, then reuses the existing exact circle-crossing implementation.
    """
    total_clusters = len(clusters)
    log_info(f"── 拓扑验证 / Topology verification ({total_clusters:,} 簇/clusters) ...")
    t0 = time.perf_counter()
    windows = _topology_point_windows(clusters, cfg)
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
        _route_progress(
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

    log_info(
        f"── 拓扑验证: {len(topologies):,} 拓扑/topologies "
        f"({time.perf_counter() - t0:.1f}s)"
    )
    return topologies, annotated_clusters, all_observations


def _detect_intersections_db(
    conn: Any,
    cfg: IntersectConfig,
    spatial_context: SpatialContext,
) -> IntersectResult:
    """DB-mode intersection detection.

    ZH: DB 模式交叉口检测。
        - Method 1: 一次加载全局逐车处理（单车操作，无需空间窗）
        - Method 3: 复用同一 tracks workspace 的全局空间索引
        - 共享 _cluster_pipeline + _build_intersect_report
    """
    # ── Spatial extent ──────────────────────────────────────────────
    extent = _query_spatial_extent(
        conn,
        table=cfg.db_table,
        kept_table=cfg.kept_table,
        source_table=cfg.source_table,
        refine_id=cfg.refine_id,
        spatial_context=spatial_context,
    )
    if extent is None:
        return _empty_intersect_result(spatial_context)

    transformer = CoordinateTransformer(spatial_context)

    live = cfg.live_status

    tracker = PerfTracker("detect_db")

    # ── Method 1: stream per-vehicle (DB-specific) ──────────────────
    if live is not None:
        live.update(stage="Detect: 流式 / Stream M1",
                    detail="Method 1: turn events...")
    t_stage = time.perf_counter()
    with tracker.stage("stream_m1"):
        turn_events, tracks = _stream_extract_method_1(
            conn, cfg, spatial_context, transformer,
            live_status=live, perf=tracker,
        )
    if live is not None:
        live.update(
            detail=f"Turn events: {len(turn_events):,}",
            elapsed=time.perf_counter() - t_stage,
        )

    # ── Method 3: one global in-memory broad phase (DB-specific) ────
    t_stage = time.perf_counter()
    with tracker.stage("cross_track_global"):
        cross_candidates = _extract_cross_track_db_workspace(
            tracks,
            cfg,
            extent,
            transformer,
            live_status=live,
            perf=tracker,
        )

    # ── Shared: cluster pipeline (G* + DBSCAN + merge + ablation) ───
    if live is not None:
        live.update(stage="Detect: 聚类 / Clustering",
                    detail="G* + DBSCAN + merge + ablation...")
    t_stage = time.perf_counter()
    with tracker.stage("cluster_pipeline"):
        hotspot_events, annotated_clusters = _cluster_pipeline(
            turn_events, cross_candidates,
            transformer, cfg,
            live_status=live, perf=tracker,
        )
    if live is not None:
        live.update(
            detail=f"Clusters: {len(annotated_clusters):,}",
            elapsed=time.perf_counter() - t_stage,
        )

    # ── DB-mode topology: reuse the single-pass tracks workspace ────
    if live is not None:
        live.update(stage="Detect: 拓扑验证 / Topology",
                    detail="Topology verification (single-pass workspace)...")
    t_stage = time.perf_counter()
    with tracker.stage("topology_index"):
        topologies, annotated_clusters, observations = _build_topologies_from_tracks(
            annotated_clusters, tracks, cfg, transformer,
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
        clusters=annotated_clusters,
        topologies=topologies,
        observations=observations,
        cross_track_candidates=cross_candidates,
        report=_build_intersect_report(
            annotated_clusters, topologies,
            turn_events, hotspot_events, cross_candidates,
            track_count=len(tracks),
        ),
        spatial_context=spatial_context,
    )


# ── Step 1: Group by vehicle ──────────────────────────────────────────


def _group_by_vehicle(
    handle: TrajectorySourceHandle,
) -> dict[str, list[TrajectoryPoint]]:
    """Consume the full stream and group points without choosing a CRS origin."""
    tracks: dict[str, list[TrajectoryPoint]] = {}

    for tp in handle.iter_points():
        if tp.vehicle_id not in tracks:
            tracks[tp.vehicle_id] = []
        tracks[tp.vehicle_id].append(tp)

    return tracks


# ═══════════════════════════════════════════════════════════════════════
# Method 1: Turn event detection + Getis-Ord G* hotspot scoring
# ═══════════════════════════════════════════════════════════════════════


def _detect_turn_events(
    tracks: dict[str, list[TrajectoryPoint]],
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

    for _track_key, track in _perf_iter(perf, tracks.items(), name="tracks_turn"):
        if len(track) < 2 * w + 1:
            continue

        xy = [_point_xy(p) for p in track]
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
                        event_calculate_index=len(events),
                        vehicle_id=p.vehicle_id,
                        timestamp=p.timestamp,
                        lon=p.lon,
                        lat=p.lat,
                        x=xy[i][0],
                        y=xy[i][1],
                        entry_bearing=in_bearing,
                        exit_bearing=out_bearing,
                        turn_deg=turn,
                        score=abs_turn,
                        source="turn",
                        point_ref=p.point_ref,
                        refined_point_ref=p.refined_point_ref,
                        source_ids=(p.point_ref.source_id,),
                    )
                )

        track_count += 1
        now = time.perf_counter()
        if track_count % 10000 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"已处理/Processed:\t{track_count:,}/{total_tracks:,}\t轨迹/tracks",
                progress=track_count / total_tracks,
                elapsed=now - t0,
                stage="Detect: 转向事件 / Turn Events",
            )
            last_t = now

    return events


def _compute_getis_ord_g_star(
    events: list[TurnEvent], radius_m: float,
    *,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> dict[int, float]:
    """Return {event_calculate_index: z_score} for G* spatial concentration.

    Uses a binary intensity model: each event contributes 1.0 to local
    count if within ``radius_m``. Since TurnEvent is frozen, z-scores
    are returned as a separate dict instead of mutating event fields.

    Uses an exact uniform grid: candidates come from a conservatively expanded,
    bounded intersecting-cell range (at most four cells per axis), then retain
    the historical squared-distance ``<=`` predicate as the exact filter. The
    grid retains only event indexes, never neighbour collections.
    """
    n = len(events)
    if n < 2:
        return {e.event_calculate_index: 0.0 for e in events}

    t0 = time.perf_counter()
    last_t = t0
    log_info(f"    计算 Getis-Ord G* / Computing G* ({n:,} 事件/events) ...")
    if not math.isfinite(radius_m) or any(not math.isfinite(e.x) or not math.isfinite(e.y) for e in events):
        # Preserve the historical IEEE comparison behaviour for inputs a grid
        # cannot represent (NaN/Inf), rather than raising a new exception.
        local_counts = _getis_local_counts_scan(events, radius_m, live_status=live_status, perf=perf, started=t0)
    else:
        local_counts = _getis_local_counts_grid(
            events, radius_m, live_status=live_status, perf=perf, started=t0,
        )

    log_info(f"    G*: {n:,} 事件/events ({time.perf_counter() - t0:.1f}s)")

    mean = sum(local_counts) / n
    variance = sum(v * v for v in local_counts) / n - mean * mean
    std = math.sqrt(max(variance, 0.0))

    if std == 0.0:
        return {e.event_calculate_index: 0.0 for e in events}

    return {
        e.event_calculate_index: (lc - mean) / std
        for e, lc in zip(events, local_counts)
    }


def _getis_local_counts_scan(
    events: list[TurnEvent], radius_m: float, *, live_status: Any = None,
    perf: PerfTracker | None = None, started: float | None = None,
) -> list[float]:
    """Historical exact comparison path for non-representable grid inputs."""
    radius_sq = radius_m * radius_m
    # The default hot path needs no instrumentation.  Keeping this compact
    # also preserves the small-input cost profile of the former inline scan.
    if perf is None and live_status is None:
        local_counts: list[float] = []
        for event in events:
            count = 0.0
            ex, ey = event.x, event.y
            for other in events:
                dx = other.x - ex
                dy = other.y - ey
                if dx * dx + dy * dy <= radius_sq:
                    count += 1.0
            local_counts.append(count)
        return local_counts
    local_counts: list[float] = []
    begin = started or time.perf_counter()
    last_t = begin
    outer_events = _perf_iter(perf, enumerate(events), name="gstar_events", total=len(events))
    for i, event in outer_events:
        count = 0.0
        if perf is None:
            for other in events:
                dx = other.x - event.x
                dy = other.y - event.y
                if dx * dx + dy * dy <= radius_sq:
                    count += 1.0
        else:
            with perf.stage("gstar_neighbors"):
                for other in events:
                    dx = other.x - event.x
                    dy = other.y - event.y
                    if dx * dx + dy * dy <= radius_sq:
                        count += 1.0
        local_counts.append(count)
        now = time.perf_counter()
        if (i + 1) % 500 == 0 or (now - last_t) >= 3.0:
            _route_progress(live_status, detail=f"G*:\t{i + 1:,}/{len(events):,}\t事件/events",
                            progress=(i + 1) / len(events), elapsed=now - begin,
                            stage="Detect: G* 热点 / Hotspot G*")
            last_t = now
    return local_counts


def _count_getis_grid_candidates(
    event: TurnEvent,
    events: list[TurnEvent],
    cells: dict[tuple[int, int], list[int]],
    bounds: tuple[int, int, int, int],
    radius_sq: float,
) -> float:
    """Count one event's exact grid candidates without per-item telemetry."""
    min_cell_x, max_cell_x, min_cell_y, max_cell_y = bounds
    count = 0.0
    for neighbor_x in range(min_cell_x, max_cell_x + 1):
        for neighbor_y in range(min_cell_y, max_cell_y + 1):
            for other_index in cells.get((neighbor_x, neighbor_y), ()):
                other = events[other_index]
                dx = other.x - event.x
                dy = other.y - event.y
                if dx * dx + dy * dy <= radius_sq:
                    count += 1.0
    return count


def _getis_local_counts_grid(
    events: list[TurnEvent], radius_m: float, *, live_status: Any = None,
    perf: PerfTracker | None = None, started: float | None = None,
    phase_timings: dict[str, float] | None = None,
) -> list[float]:
    """Exact radius counts without allocating neighbour lists or NxN matrices."""
    cell_size = abs(radius_m)
    if cell_size == 0.0 or len(events) <= 256:
        return _getis_local_counts_scan(events, radius_m, live_status=live_status, perf=perf, started=started)
    # Finite operands can still produce an infinite cell quotient for a tiny
    # positive radius.  floor(inf) would newly raise; use the historical scan.
    if any(not math.isfinite(event.x / cell_size) or not math.isfinite(event.y / cell_size)
           or not math.isfinite((event.x - cell_size) / cell_size)
           or not math.isfinite((event.x + cell_size) / cell_size)
           or not math.isfinite((event.y - cell_size) / cell_size)
           or not math.isfinite((event.y + cell_size) / cell_size)
           for event in events):
        return _getis_local_counts_scan(events, radius_m, live_status=live_status, perf=perf, started=started)
    radius_sq = radius_m * radius_m
    if not math.isfinite(radius_sq):
        # Historical squared-distance arithmetic overflows to infinity; its
        # resulting IEEE comparison cannot be bounded by finite grid cells.
        return _getis_local_counts_scan(events, radius_m, live_status=live_status, perf=perf, started=started)
    cell_bounds: list[tuple[int, int, int, int]] = []
    for event in events:
        min_cell_x = math.floor(math.nextafter((event.x - cell_size) / cell_size, -math.inf))
        max_cell_x = math.floor(math.nextafter((event.x + cell_size) / cell_size, math.inf))
        min_cell_y = math.floor(math.nextafter((event.y - cell_size) / cell_size, -math.inf))
        max_cell_y = math.floor(math.nextafter((event.y + cell_size) / cell_size, math.inf))
        # A one-radius interval normally intersects at most four cells after
        # outward nextafter expansion.  At huge coordinates a single quotient
        # ULP can span ~1e292 integer cells; never attempt to enumerate it.
        if max_cell_x - min_cell_x + 1 > 4 or max_cell_y - min_cell_y + 1 > 4:
            return _getis_local_counts_scan(events, radius_m, live_status=live_status, perf=perf, started=started)
        cell_bounds.append((min_cell_x, max_cell_x, min_cell_y, max_cell_y))
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    build_started = time.perf_counter()
    for index, event in enumerate(events):
        cells[(math.floor(event.x / cell_size), math.floor(event.y / cell_size))].append(index)
    if phase_timings is not None:
        phase_timings["grid_build_seconds"] = time.perf_counter() - build_started

    local_counts: list[float] = []
    count_started = time.perf_counter()
    last_t = started or time.perf_counter()
    for i, event in _perf_iter(perf, enumerate(events), name="gstar_events", total=len(events)):
        # Do not assume a fixed +/- one-cell stencil.  Floating-point
        # subtraction can round an exact radius crossing across a cell seam
        # (notably x=-tiny versus x=radius).  Expand the inclusive coordinate
        # interval outward by one representable value, then retain the legacy
        # squared-distance predicate as the exact final filter.
        # Apply nextafter after division: applying it before division can
        # underflow back to signed zero (0 / cell_size), losing cell -1.
        if perf is None:
            count = _count_getis_grid_candidates(
                event, events, cells, cell_bounds[i], radius_sq,
            )
        else:
            # Keep the historical telemetry name while timing one complete
            # event-neighbour block instead of every candidate iteration.
            with perf.stage("gstar_neighbors"):
                count = _count_getis_grid_candidates(
                    event, events, cells, cell_bounds[i], radius_sq,
                )
        local_counts.append(count)
        now = time.perf_counter()
        if (i + 1) % 500 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"G*:\t{i + 1:,}/{len(events):,}\t事件/events",
                progress=(i + 1) / len(events),
                elapsed=now - (started or now),
                stage="Detect: G* 热点 / Hotspot G*",
            )
            last_t = now
    if phase_timings is not None:
        phase_timings["radius_count_seconds"] = time.perf_counter() - count_started
    return local_counts


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
        replace(e, g_star_z=z_scores.get(e.event_calculate_index, 0.0))
        for e in events
        if z_scores.get(e.event_calculate_index, 0.0) >= cfg.g_star_min_z
        and abs(e.turn_deg) >= cfg.min_turn_deg
    ]
    log_info(f"── 热点筛选: {len(result):,} 事件/events ({time.perf_counter() - t0:.1f}s)")
    return result


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# DBSCAN clustering (shared by turn and cross-track methods)
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


def _build_dbscan_grid(
    xs: list[float],
    ys: list[float],
    eps_m: float,
) -> dict[tuple[int, int], list[int]] | None:
    """Build an exact, bounded-per-query spatial index for DBSCAN.

    ``None`` deliberately selects the historical full scan for invalid/private
    boundary inputs.  That preserves the legacy exception/label behaviour for
    zero or negative eps, unequal coordinate vectors, and non-finite values.
    """
    # ``eps_sq`` is also part of the historical predicate.  If it overflows,
    # the scan treats many infinite squared distances as neighbours; retain
    # that private edge behaviour rather than attempting a grid approximation.
    if (eps_m <= 0.0 or not math.isfinite(eps_m) or not math.isfinite(eps_m * eps_m)
            or len(xs) != len(ys)):
        return None
    grid: dict[tuple[int, int], list[int]] = {}
    for index, (x, y) in enumerate(zip(xs, ys)):
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        # Adjacent-cell enumeration is only safe when the conservatively
        # rounded query interval remains a tiny, enumerable span.  At very
        # large finite coordinates (for example 1e308 with eps=1), one ULP is
        # enormously wider than eps and would otherwise create a practically
        # unbounded ``range``.  Preserve historical scan semantics instead.
        if (_dbscan_grid_axis_bounds(x, eps_m) is None
                or _dbscan_grid_axis_bounds(y, eps_m) is None):
            return None
        try:
            cell = (math.floor(x / eps_m), math.floor(y / eps_m))
        except OverflowError:
            return None
        grid.setdefault(cell, []).append(index)
    return grid


def _dbscan_grid_axis_bounds(value: float, eps_m: float) -> tuple[int, int] | None:
    """Return a conservative, safely enumerable cell interval for one axis."""
    try:
        low = math.floor(math.nextafter(value - eps_m, -math.inf) / eps_m)
        high = math.floor(math.nextafter(value + eps_m, math.inf) / eps_m)
    except (OverflowError, ValueError):
        return None
    if high < low or high - low > 8:
        return None
    return low, high


def _grid_neighbors_indices(
    xs: list[float],
    ys: list[float],
    index: int,
    eps_m: float,
    grid: dict[tuple[int, int], list[int]],
) -> list[int]:
    """Return exact neighbours in original-index order from all overlapping cells.

    The usual ``+-1`` cell shortcut is not safe at a floating-point cell
    boundary.  For example, ``-1e-300`` and ``1.0`` are exactly one ``eps``
    apart, while their floor cells differ by two.  Expand arithmetic bounds by
    one ULP before flooring; the original squared-distance predicate removes
    the small number of conservative false candidates.
    """
    base_x, base_y = xs[index], ys[index]
    eps_sq = eps_m * eps_m
    result: list[int] = []
    x_bounds = _dbscan_grid_axis_bounds(base_x, eps_m)
    y_bounds = _dbscan_grid_axis_bounds(base_y, eps_m)
    if x_bounds is None or y_bounds is None:
        raise RuntimeError("DBSCAN grid invariant violated after construction")
    low_x, high_x = x_bounds
    low_y, high_y = y_bounds
    for grid_x in range(low_x, high_x + 1):
        for grid_y in range(low_y, high_y + 1):
            for candidate in grid.get((grid_x, grid_y), ()):
                dx = xs[candidate] - base_x
                dy = ys[candidate] - base_y
                if dx * dx + dy * dy <= eps_sq:
                    result.append(candidate)
    result.sort()
    return result


def _grid_has_min_neighbors(
    xs: list[float],
    ys: list[float],
    index: int,
    eps_m: float,
    min_samples: int,
    grid: dict[tuple[int, int], list[int]],
) -> bool:
    """Return whether ``index`` is core without materialising its neighbours.

    DBSCAN only needs the complete neighbour list when expanding a frontier.
    Core classification needs a count up to ``min_samples`` and can stop as
    soon as that threshold is reached.  This distinction is important for
    cross-track candidates: a dense intersection can contain tens of thousands
    of points, while the configured threshold is normally three.
    """
    if min_samples <= 0:
        return True
    base_x, base_y = xs[index], ys[index]
    eps_sq = eps_m * eps_m
    x_bounds = _dbscan_grid_axis_bounds(base_x, eps_m)
    y_bounds = _dbscan_grid_axis_bounds(base_y, eps_m)
    if x_bounds is None or y_bounds is None:
        raise RuntimeError("DBSCAN grid invariant violated after construction")
    count = 0
    for grid_x in range(x_bounds[0], x_bounds[1] + 1):
        for grid_y in range(y_bounds[0], y_bounds[1] + 1):
            for candidate in grid.get((grid_x, grid_y), ()):
                dx = xs[candidate] - base_x
                dy = ys[candidate] - base_y
                if dx * dx + dy * dy <= eps_sq:
                    count += 1
                    if count >= min_samples:
                        return True
    return False


def _take_grid_frontier_neighbors(
    xs: list[float],
    ys: list[float],
    index: int,
    eps_m: float,
    grid: dict[tuple[int, int], list[int]],
    available: dict[tuple[int, int], set[int]],
    point_cells: list[tuple[int, int]],
) -> list[int]:
    """Take exact, not-yet-queued neighbours in historical index order.

    Removing a point from ``available`` when it enters the current expansion
    queue is equivalent to the old per-cluster ``queued_epoch`` check.  The
    difference is that later core points inspect only the shrinking frontier,
    rather than repeatedly walking every already-labelled member of a dense
    cluster.
    """
    base_x, base_y = xs[index], ys[index]
    eps_sq = eps_m * eps_m
    x_bounds = _dbscan_grid_axis_bounds(base_x, eps_m)
    y_bounds = _dbscan_grid_axis_bounds(base_y, eps_m)
    if x_bounds is None or y_bounds is None:
        raise RuntimeError("DBSCAN grid invariant violated after construction")
    result: list[int] = []
    for grid_x in range(x_bounds[0], x_bounds[1] + 1):
        for grid_y in range(y_bounds[0], y_bounds[1] + 1):
            for candidate in available.get((grid_x, grid_y), ()):
                dx = xs[candidate] - base_x
                dy = ys[candidate] - base_y
                if dx * dx + dy * dy <= eps_sq:
                    result.append(candidate)
    result.sort()
    for candidate in result:
        available[point_cells[candidate]].remove(candidate)
    return result


def _run_dbscan_large_dispatch(
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

    grid = _build_dbscan_grid(xs, ys, eps_m)
    if grid is None:
        # Preserve private boundary behaviour for invalid/non-finite inputs.
        # These cases cannot be represented safely by the spatial index.
        return _run_dbscan_scan(
            xs, ys, eps_m=eps_m, min_samples=min_samples,
            live_status=live_status, stage=stage, perf=perf,
        )

    labels = [_UNVISITED] * n
    cluster_label = 0
    core_flags: list[bool | None] = [None] * n
    point_cells: list[tuple[int, int]] = [(0, 0)] * n
    available: dict[tuple[int, int], set[int]] = {}
    for cell, indexes in grid.items():
        available[cell] = set(indexes)
        for index in indexes:
            point_cells[index] = cell
    t0 = time.perf_counter()
    last_t = t0

    for i in _perf_iter(perf, range(n), name="dbscan_points", total=n):
        if labels[i] != _UNVISITED:
            continue
        is_core = _grid_has_min_neighbors(
            xs, ys, i, eps_m, min_samples, grid,
        )
        core_flags[i] = is_core
        if not is_core:
            labels[i] = _NOISE
            continue

        labels[i] = cluster_label
        available[point_cells[i]].remove(i)
        seeds = _take_grid_frontier_neighbors(
            xs, ys, i, eps_m, grid, available, point_cells,
        )
        while seeds:
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_label
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_label
            is_core = core_flags[current]
            if is_core is None:
                is_core = _grid_has_min_neighbors(
                    xs, ys, current, eps_m, min_samples, grid,
                )
                core_flags[current] = is_core
            if is_core:
                if perf is None:
                    seeds.extend(_take_grid_frontier_neighbors(
                        xs, ys, current, eps_m, grid, available, point_cells,
                    ))
                else:
                    with perf.stage("dbscan_seed_neighbors"):
                        seeds.extend(_take_grid_frontier_neighbors(
                            xs, ys, current, eps_m, grid, available, point_cells,
                        ))
        cluster_label += 1
        now = time.perf_counter()
        if i % 500 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"DBSCAN:\t{i + 1:,}/{n:,}\t点/points,\t{cluster_label}\t簇/clusters",
                progress=(i + 1) / n,
                elapsed=now - t0,
                stage=f"Detect: DBSCAN ({stage})",
            )
            last_t = now

    log_info(f"    DBSCAN: {n:,} 点/points → {cluster_label:,} 簇/clusters ({time.perf_counter() - t0:.1f}s)")
    return labels


def _run_dbscan_scan(
    xs: list[float],
    ys: list[float],
    *,
    eps_m: float,
    min_samples: int,
    live_status: Any = None,
    stage: str = "DBSCAN",
    perf: PerfTracker | None = None,
) -> list[int]:
    """Historical full-scan DBSCAN for tiny or non-indexable inputs."""
    n = len(xs)
    labels = [_UNVISITED] * n
    cluster_label = 0
    t0 = time.perf_counter()
    last_t = t0
    for i in _perf_iter(perf, range(n), name="dbscan_points", total=n):
        if labels[i] != _UNVISITED:
            continue
        neighbors = _neighbors_indices(xs, ys, i, eps_m)
        if len(neighbors) < min_samples:
            labels[i] = _NOISE
            continue
        labels[i] = cluster_label
        seeds = [nb for nb in neighbors if nb != i]
        while seeds:
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_label
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_label
            cur_neighbors = _neighbors_indices(xs, ys, current, eps_m)
            if len(cur_neighbors) >= min_samples:
                if perf is None:
                    for nb in cur_neighbors:
                        if labels[nb] in (_UNVISITED, _NOISE) and nb not in seeds:
                            seeds.append(nb)
                else:
                    with perf.stage("dbscan_seed_neighbors"):
                        for nb in cur_neighbors:
                            if labels[nb] in (_UNVISITED, _NOISE) and nb not in seeds:
                                seeds.append(nb)
        cluster_label += 1
        now = time.perf_counter()
        if i % 500 == 0 or (now - last_t) >= 3.0:
            _route_progress(
                live_status,
                detail=f"DBSCAN:\t{i + 1:,}/{n:,}\t点/points,\t{cluster_label}\t簇/clusters",
                progress=(i + 1) / n,
                elapsed=now - t0,
                stage=f"Detect: DBSCAN ({stage})",
            )
            last_t = now
    log_info(f"    DBSCAN: {n:,} 点/points → {cluster_label:,} 簇/clusters ({time.perf_counter() - t0:.1f}s)")
    return labels


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
    """Run the compact historical scan for small inputs and grid path otherwise."""
    n = len(xs)
    if n == 0:
        return []
    if n > 256:
        return _run_dbscan_large_dispatch(
            xs, ys, eps_m=eps_m, min_samples=min_samples,
            live_status=live_status, stage=stage, perf=perf,
        )

    return _run_dbscan_scan(
        xs, ys, eps_m=eps_m, min_samples=min_samples,
        live_status=live_status, stage=stage, perf=perf,
    )


def _group_cluster_items(items: list[Any], labels: list[int]) -> list[tuple[int, list[Any]]]:
    """Group non-noise DBSCAN items once while preserving legacy ordering."""
    grouped: dict[int, list[Any]] = defaultdict(list)
    for index, label in enumerate(labels):
        if label >= 0:
            grouped[label].append(items[index])
    return [
        (cluster_label, grouped[cluster_label])
        for cluster_label in sorted(grouped)
    ]


def _cluster_dbscan(
    events: list[TurnEvent],
    transformer: CoordinateTransformer,
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
    for cid, cluster_items in _group_cluster_items(events, labels):
        if not cluster_items:
            continue
        cx = sum(e.x for e in cluster_items) / len(cluster_items)
        cy = sum(e.y for e in cluster_items) / len(cluster_items)
        radius = max(_distance_m((cx, cy), (e.x, e.y)) for e in cluster_items)
        lon, lat = transformer.inverse(cx, cy)
        cluster = HotspotCluster(
            cluster_calculate_index=cid,
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


def _cluster_orthogonal_cross_track_candidates(
    candidates: list[CrossTrackCandidate],
    transformer: CoordinateTransformer,
    *,
    eps_m: float,
    min_samples: int,
    live_status: Any = None,
    perf: PerfTracker | None = None,
) -> list[HotspotCluster]:
    """DBSCAN-cluster orthogonal_cross_track candidates, wrapping as TurnEvents.

    ZH: 将多车正交候选点包装为 ``TurnEvent(source="orthogonal_cross_track")``
        后 DBSCAN 聚类。
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
    for cid, cluster_items in _group_cluster_items(candidates, labels):
        if not cluster_items:
            continue
        cx = sum(c.x for c in cluster_items) / len(cluster_items)
        cy = sum(c.y for c in cluster_items) / len(cluster_items)
        radius = max(_distance_m((cx, cy), (c.x, c.y)) for c in cluster_items)
        lon, lat = transformer.inverse(cx, cy)
        cluster_events = tuple(
            TurnEvent(
                event_calculate_index=i,
                vehicle_id=c.vehicle_id,
                timestamp=c.timestamp,
                lon=c.lon,
                lat=c.lat,
                x=c.x,
                y=c.y,
                entry_bearing=0.0,
                exit_bearing=0.0,
                turn_deg=0.0,
                score=0.0,
                source="orthogonal_cross_track",
                point_ref=c.point_ref,
                cross_track_calculate_index=c.cross_track_calculate_index,
                source_ids=tuple(sorted({
                    c.from_segment_start_point_ref.source_id,
                    c.to_segment_start_point_ref.source_id,
                })),
            )
            for i, c in enumerate(cluster_items)
        )
        cluster = HotspotCluster(
            cluster_calculate_index=cid,
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
# Cluster merge (deduplicate overlapping turn and cross-track clusters)
# ═══════════════════════════════════════════════════════════════════════


def _merge_clusters(
    clusters: list[HotspotCluster],
    merge_distance_m: float,
    transformer: CoordinateTransformer,
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
        merged_source_refs = {
            source: list(items)
            for source, items in _cluster_source_point_refs(ci).items()
        }
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
                    _extend_ref_lists(
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

        new_lon, new_lat = transformer.inverse(new_cx, new_cy)
        merged_cluster = HotspotCluster(
            cluster_calculate_index=ci.cluster_calculate_index,
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
                source_point_refs={
                    source: tuple(items)
                    for source, items in merged_source_refs.items()
                },
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
        _route_progress(
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
