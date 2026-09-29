"""Detect candidate batches, identity memberships and topology verification."""
from __future__ import annotations
import heapq
import math
import time
from array import array
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable
import numpy as np
from ..models import (TrackModel, RuntimeConfig, SpatialContext, RefinementSubtrack, TurnBatch, CrossCandidateBatch, MemberBatch, ObservationBatch, HotspotCluster, IntersectionTopology, IntersectReport)
from ..projection import CoordinateTransformer
from ..exceptions import V2Error
from ..candidates.turn_event_relation import TurnEventRelation
from ..candidates.cross_track_relation import CrossCandidateRelation
from ..candidates.detect_turn_events import empty_turn_batch, concatenate_turn_batches
from ..candidates.detect_cross_track_pairs import empty_cross_batch
from ..candidates.collect_candidates import extract_turn_candidates, extract_cross_track_candidates
from ..candidates.build_patches import iter_track_patches
from ..candidates.detect_grid_crossings import run_grid_cross_track_task
from ..candidates.dbscan_index import ActiveGridLinks, index_typecode
from ..telemetry.console_logging import log_info
from ..telemetry.heartbeat import heartbeat_progress, operation_heartbeat
from .performance_timing import PerfTracker
from .progress import report_detect_progress
from .extract_topology import _build_topologies_from_tracks
from ..trajectories.query_refine_points import fetch_refine_run_row
from ..db.artifact_registry import canonical_json

def _perf_iter(perf, iterable, *, name, total=None, track=True):
    return perf.timed_iter(iterable,name=name,total=total,track=track) if perf is not None else iterable

_NOISE=-1
_UNVISITED=-2

@dataclass(frozen=True)
class IntersectConfig(TrackModel):
    """Detect configuration for turn and cross-track detection.

    PRD v2 Detect §7
    """

    # ── Mode toggles ───────────────────────────────────────────────
    use_turn_events: bool
    use_cross_track_candidates: bool

    # ── Turn event detection (Method 1) ────────────────────────────
    turn_window: int
    min_turn_deg: float
    max_turn_deg: float
    min_leg_m: float
    min_movement_speed_mps: float

    # ── Getis-Ord G* hotspot scoring ───────────────────────────────
    g_star_radius_m: float
    g_star_min_z: float

    # ── DBSCAN (all candidate points together) ────────────────────
    candidate_dbscan_eps_m: float
    candidate_dbscan_min_samples: int

    # ── Cross-track orthogonal detection (Method 3) ──────────────────
    cross_track_min_avg_speed: float
    cross_track_straightness_deg: float
    cross_track_min_angle_deg: float
    cross_track_max_angle_deg: float
    # Structural memory bounds owned by candidates.build_patches.
    cross_track_grid_cell_m: float
    cross_track_pair_batch_size: int
    cross_track_max_grid_cells_per_segment: int

    # ── Ablation centroid matching ────────────────────────────────
    merge_centroid_distance_m: float

    # ── Topology verification (shared) ─────────────────────────────
    min_intersection_radius_m: float
    topology_radius_padding_m: float
    branch_threshold_deg: float
    min_branches: int
    min_observations: int
    min_directed_transitions: int
    bearing_window: int

    # ── Spatial sliding window (DB mode) ───────────────────────────
    spatial_window_size: tuple[float, float]
    spatial_window_overlap: float
    stream_batch_size: int
    cross_track_task_grid_m: float
    cross_track_task_threads: int


@dataclass(frozen=True)
class DetectRuntimeContext:
    """Per-run dependencies and identity; never loaded from static TOML."""

    query_active_from_db: bool
    db_table: str
    kept_table: str | None
    source_table: str | None
    refine_id: str | None
    live_status: Any


@dataclass(frozen=True)
class _IntersectRunConfig(IntersectConfig):
    """Internal view combining immutable tuning with one run's context."""

    query_active_from_db: bool
    db_table: str
    kept_table: str | None
    source_table: str | None
    refine_id: str | None
    live_status: Any


@dataclass(frozen=True)
class IntersectResult(TrackModel):
    turn_events: TurnBatch | TurnEventRelation
    hotspot_events: TurnBatch
    clusters: list[HotspotCluster]
    topologies: list[IntersectionTopology]
    report: IntersectReport
    spatial_context: SpatialContext
    observations: list[ObservationBatch] = field(default_factory=list)
    cross_track_candidates: CrossCandidateBatch | CrossCandidateRelation = field(default_factory=empty_cross_batch)

    def close(self):
        try:
            close=getattr(self.turn_events,"close",None)
            if close: close()
        finally:
            close=getattr(self.cross_track_candidates,"close",None)
            if close: close()

    def get_report(self): return self.report


def _batches(events):
    if isinstance(events,(TurnBatch,CrossCandidateBatch)):
        yield events
    else:
        iterator=events.iter_batches()
        try:
            yield from iterator
        finally:
            close=getattr(iterator,"close",None)
            if close: close()


def _coordinates(events, *, include_sources: bool = True):
    if isinstance(events,(TurnBatch,CrossCandidateBatch)):
        source = (events.source_id if isinstance(events,TurnBatch) else events.from_source_id) if include_sources else None
        return events.xy,events.calculation_index,source
    size = len(events)
    xy = np.empty((size, 2), dtype=np.float64)
    indices = np.empty(size, dtype=np.int64)
    sources = np.empty(size, dtype=object) if include_sources else None
    cursor = 0
    batches = events.iter_coordinate_batches(include_source=include_sources)
    try:
        for columns in batches:
            stop = cursor + len(columns[1])
            xy[cursor:stop] = columns[0]
            indices[cursor:stop] = columns[1]
            if include_sources:
                sources[cursor:stop] = columns[2]
            cursor = stop
    finally:
        batches.close()
    if cursor != size:
        raise ValueError("event relation coordinate count changed during scan")
    return xy, indices, sources


def _compute_getis_ord_g_star(events, radius_m, **_kwargs):
    started = time.monotonic()
    xy,indices,_=_coordinates(events,include_sources=False)
    n=len(xy)
    counts=np.zeros(n,dtype=np.float64)
    radius_sq=radius_m*radius_m
    finite=np.isfinite(xy).all() and math.isfinite(radius_m)
    cells=defaultdict(list)
    indexed=finite and radius_m!=0 and n>256
    if indexed:
        cell=abs(radius_m)
        quotients=xy/cell
        indexed=np.isfinite(quotients).all() and np.isfinite((xy-cell)/cell).all() and np.isfinite((xy+cell)/cell).all()
        if indexed:
            indexed = all(_dbscan_grid_axis_bounds(float(value), cell) is not None for value in xy.flat)
        if indexed:
            for i,(x,y) in enumerate(quotients): cells[(math.floor(x),math.floor(y))].append(i)
    for i in _perf_iter(_kwargs.get("perf"), range(n), name="gstar_events", total=n):
        if indexed:
            x,y=xy[i]
            xmin=math.floor(math.nextafter((x-cell)/cell,-math.inf)); xmax=math.floor(math.nextafter((x+cell)/cell,math.inf))
            ymin=math.floor(math.nextafter((y-cell)/cell,-math.inf)); ymax=math.floor(math.nextafter((y+cell)/cell,math.inf))
            candidates=(j for cx in range(xmin,xmax+1) for cy in range(ymin,ymax+1) for j in cells.get((cx,cy),()))
        else: candidates=range(n)
        counts[i]=sum(1 for j in _perf_iter(_kwargs.get("perf"), candidates, name="gstar_neighbors") if (xy[j,0]-xy[i,0])**2+(xy[j,1]-xy[i,1])**2<=radius_sq)
        if (i + 1) % 500 == 0 and _kwargs.get("live_status") is not None:
            _kwargs["live_status"].update(detail=f"G*：{i+1}/{n} 事件", stage="Detect: G*", elapsed=time.monotonic()-started)
    if _kwargs.get("live_status") is not None:
        _kwargs["live_status"].update(detail=f"G*：{n}/{n} 事件", stage="Detect: G*", elapsed=time.monotonic()-started)
    mean=float(counts.mean()) if n else 0
    variance=float(np.mean(counts*counts)-mean*mean) if n else 0
    std=math.sqrt(max(variance,0))
    z=(counts-mean)/std if std else np.zeros(n)
    return {int(index):float(value) for index,value in zip(indices,z)}


def _select_hotspot_events(events,cfg,**kwargs):
    scores=_compute_getis_ord_g_star(events,cfg.g_star_radius_m,**kwargs)
    pieces=[]
    for batch in _batches(events):
        z=np.fromiter((scores[int(index)] for index in batch.calculation_index),dtype=np.float64,count=len(batch))
        keep=np.flatnonzero((z>=cfg.g_star_min_z) & (np.abs(batch.turn_deg)>=cfg.min_turn_deg))
        pieces.append(replace(batch.take(keep),g_star_z=z[keep],g_star_z_valid=np.ones(len(keep),dtype=np.bool_)))
    return concatenate_turn_batches(pieces)


def _cluster_dbscan(events,transformer,*,eps_m,min_samples,source="turn",**kwargs):
    xy,indices,sources=_coordinates(events)
    labels=np.asarray(_run_dbscan(xy[:,0],xy[:,1],eps_m=eps_m,min_samples=min_samples,**kwargs),dtype=np.int64)
    clusters=[]
    for cid in np.unique(labels[labels>=0]):
        positions=np.flatnonzero(labels==cid)
        center=xy[positions].mean(axis=0); lon,lat=transformer.inverse(*center)
        members=MemberBatch(np.full(len(positions),source,dtype=object),indices[positions],np.arange(len(positions),dtype=np.int64),sources[positions],np.ones(len(positions),dtype=np.bool_))
        clusters.append(HotspotCluster(cluster_calculate_index=int(cid),centroid_x=float(center[0]),centroid_y=float(center[1]),centroid_lon=lon,centroid_lat=lat,radius_m=float(np.linalg.norm(xy[positions]-center,axis=1).max()),cluster_ref=f"cluster_{source}_{cid}",members=members,source=source,algorithm_sources=(source,),source_ids=tuple(sorted(set(str(value) for value in sources[positions]))),source_counts={source:len(positions)},source_ratios={source:1.0}))
    return clusters


def _cluster_orthogonal_cross_track_candidates(candidates,transformer,**kwargs):
    clusters = _cluster_dbscan(candidates,transformer,source="orthogonal_cross_track",**kwargs)
    source_ids = [set(cluster.source_ids) for cluster in clusters]
    source_batches = candidates.iter_source_batches() if hasattr(candidates,"iter_source_batches") else ((candidates.calculation_index,candidates.from_source_id,candidates.to_source_id),)
    try:
        for indices, from_source, to_source in source_batches:
            for index, cluster in enumerate(clusters):
                selected = np.isin(indices, cluster.members.calculation_index)
                source_ids[index].update(str(value) for value in np.unique(from_source[selected]))
                source_ids[index].update(str(value) for value in np.unique(to_source[selected]))
    finally:
        close = getattr(source_batches,"close",None)
        if close is not None:
            close()
    return [replace(cluster,source_ids=tuple(sorted(source_ids[index]))) for index,cluster in enumerate(clusters)]


def _merge_count_dicts(a,b):
    result=dict(a)
    for key,value in b.items(): result[key]=result.get(key,0)+value
    return result


def _merge_clusters(clusters,merge_distance_m,transformer,**_kwargs):
    remaining=sorted(clusters,key=lambda c:len(c.members) if c.members is not None else 0,reverse=True); kept=[]
    while remaining:
        ci=remaining.pop(0); pieces=[ci.members] if ci.members is not None else []; counts=dict(ci.source_counts); sources=set(ci.source_ids)
        total=len(ci.members) if ci.members is not None else 0; cx_sum=ci.centroid_x*total; cy_sum=ci.centroid_y*total; radius=ci.radius_m
        changed=True
        while changed:
            changed=False; survivors=[]
            cx=cx_sum/total if total else ci.centroid_x; cy=cy_sum/total if total else ci.centroid_y
            for cj in remaining:
                distance=math.hypot(cx-cj.centroid_x,cy-cj.centroid_y)
                if distance<=merge_distance_m:

                    if cj.members is not None: pieces.append(cj.members)
                    counts=_merge_count_dicts(counts,cj.source_counts); sources.update(cj.source_ids)
                    radius=max(radius,distance+cj.radius_m); amount=len(cj.members) if cj.members is not None else 0
                    cx_sum+=cj.centroid_x*amount; cy_sum+=cj.centroid_y*amount; total+=amount; changed=True
                else: survivors.append(cj)
            remaining=survivors
        cx=cx_sum/total if total else ci.centroid_x; cy=cy_sum/total if total else ci.centroid_y
        lon,lat=transformer.inverse(cx,cy)
        members=MemberBatch(**{item.name:np.arange(total,dtype=np.int64) if item.name=="member_order" else np.concatenate([getattr(piece,item.name) for piece in pieces]) if pieces else np.empty(0,dtype=np.int64 if item.name in {"calculation_index","member_order"} else np.bool_ if item.name=="source_id_valid" else object) for item in fields(MemberBatch)})
        kinds=tuple(sorted(counts)); label=kinds[0] if len(kinds)==1 else "merged"
        kept.append(replace(ci,centroid_x=cx,centroid_y=cy,centroid_lon=lon,centroid_lat=lat,radius_m=radius,members=members,source=label,source_counts=counts,source_ratios=_ratio_dict(counts),algorithm_sources=kinds,source_ids=tuple(sorted(sources))))
    return kept


def _cluster_from_points(point_sources,transformer,*,eps_m,min_samples,**kwargs):
    """Cluster all enabled candidate sources in one DBSCAN pass."""
    point_sources=tuple((kind,events) for kind,events in point_sources if len(events))
    total=sum(len(events) for _,events in point_sources)
    if not total:
        return []
    xy=np.empty((total,2),dtype=np.float64)
    calculation_indices=np.empty(total,dtype=np.int64)
    point_source_ids=np.empty(total,dtype=object)
    point_kinds=np.empty(total,dtype=object)
    secondary_sources=[]
    offset=0
    for kind,events in point_sources:
        stop=offset+len(events)
        source_xy,source_indices,from_source_ids=_coordinates(events)
        xy[offset:stop]=source_xy
        calculation_indices[offset:stop]=source_indices
        point_source_ids[offset:stop]=from_source_ids
        point_kinds[offset:stop]=kind
        if hasattr(events,"iter_source_batches") or hasattr(events,"to_source_id"):
            secondary_sources.append((events,calculation_indices[offset:stop],offset))
        offset=stop
    del source_xy,source_indices,from_source_ids
    labels=np.asarray(_run_dbscan(xy[:,0],xy[:,1],eps_m=eps_m,min_samples=min_samples,**kwargs),dtype=np.int64)
    secondary_ids=defaultdict(set)
    for events,expected_indices,offset in secondary_sources:
        batches=events.iter_source_batches() if hasattr(events,"iter_source_batches") else ((events.calculation_index,events.from_source_id,events.to_source_id),)
        cursor=0
        try:
            for batch_indices,_,to_source_ids in batches:
                stop=cursor+len(batch_indices)
                if not np.array_equal(batch_indices,expected_indices[cursor:stop]):
                    raise ValueError("candidate relation source order changed during scan")
                for label,source_id in zip(labels[offset+cursor:offset+stop],to_source_ids):
                    if label>=0:
                        secondary_ids[int(label)].add(str(source_id))
                cursor=stop
        finally:
            close=getattr(batches,"close",None)
            if close is not None:
                close()
        if cursor!=len(expected_indices):
            raise ValueError("candidate relation source count changed during scan")
    clusters=[]
    for cid in np.unique(labels[labels>=0]):
        positions=np.flatnonzero(labels==cid)
        center=xy[positions].mean(axis=0)
        lon,lat=transformer.inverse(*center)
        member_kinds=point_kinds[positions]
        counts={str(kind):int(np.count_nonzero(member_kinds==kind)) for kind in np.unique(member_kinds)}
        sources=set(str(value) for value in point_source_ids[positions])
        sources.update(secondary_ids[int(cid)])
        members=MemberBatch(member_kinds,calculation_indices[positions],np.arange(len(positions),dtype=np.int64),point_source_ids[positions],np.ones(len(positions),dtype=np.bool_))
        algorithm_sources=tuple(sorted(counts))
        clusters.append(HotspotCluster(cluster_calculate_index=int(cid),centroid_x=float(center[0]),centroid_y=float(center[1]),centroid_lon=lon,centroid_lat=lat,radius_m=float(np.linalg.norm(xy[positions]-center,axis=1).max()),cluster_ref=f"cluster_candidates_{cid}",members=members,source=algorithm_sources[0] if len(algorithm_sources)==1 else "merged",algorithm_sources=algorithm_sources,source_ids=tuple(sorted(sources)),source_counts=counts,source_ratios=_ratio_dict(counts)))
    return clusters


def _build_candidate_clusters(point_sources,cfg,transformer,*,disabled_sources=None,**kwargs):
    disabled=disabled_sources or set()
    return _cluster_from_points(((kind,events) for kind,events in point_sources if kind not in disabled),transformer,eps_m=cfg.candidate_dbscan_eps_m,min_samples=cfg.candidate_dbscan_min_samples,**kwargs)


def _annotate_source_ablation(clusters,point_sources,cfg,transformer,**kwargs):
    point_sources=tuple(point_sources)
    sources=sorted({kind for kind,events in point_sources if len(events)} |
                   {kind for cluster in clusters for kind in cluster.algorithm_sources})
    without={}
    for source in sources:
        # Each pass jointly reclusters every source except this one.
        rebuilt=_build_candidate_clusters(point_sources,cfg,transformer,disabled_sources={source},**kwargs)
        without[source]=np.asarray([(item.centroid_x,item.centroid_y) for item in rebuilt],dtype=np.float64).reshape(-1,2)
        del rebuilt
    return [replace(cluster,critical_sources=tuple(source for source in cluster.algorithm_sources if not any(math.hypot(cluster.centroid_x-other[0],cluster.centroid_y-other[1])<=cfg.merge_centroid_distance_m for other in without[source]))) for cluster in clusters]


def _cluster_pipeline(turn_events,cross_candidates,transformer,cfg,**kwargs):
    hotspot=_select_hotspot_events(turn_events,cfg,**kwargs) if cfg.use_turn_events and len(turn_events) else empty_turn_batch()
    point_sources=(("turn",hotspot),("orthogonal_cross_track",cross_candidates))
    clusters=_build_candidate_clusters(point_sources,cfg,transformer,**kwargs)
    clusters=[replace(cluster,cluster_calculate_index=i) for i,cluster in enumerate(clusters)]
    return hotspot,_annotate_source_ablation(clusters,point_sources,cfg,transformer,**kwargs)


def _ratio_dict(counts):
    total=sum(counts.values())
    return {key:value/total for key,value in sorted(counts.items())} if total else {}


def _build_intersect_report(clusters,topologies,turn_events,hotspot_events,cross_candidates,*,track_count):
    complete={item.cluster_calculate_index for item in topologies if item.is_complete}
    counts={}; critical={}
    for cluster in clusters:
        if cluster.cluster_calculate_index in complete:
            counts=_merge_count_dicts(counts,cluster.source_counts)
            for source in cluster.critical_sources: critical[source]=critical.get(source,0)+1
    return IntersectReport(track_count,len(turn_events),len(hotspot_events),len(cross_candidates),len(clusters),sum("turn" in c.algorithm_sources for c in clusters),sum("orthogonal_cross_track" in c.algorithm_sources for c in clusters),len(complete),counts,_ratio_dict(counts),critical,{key:value/max(1,len(complete)) for key,value in sorted(critical.items())})


def _empty_intersect_result(spatial_context):
    return IntersectResult(empty_turn_batch(),empty_turn_batch(),[],[],_build_intersect_report([],[],empty_turn_batch(),empty_turn_batch(),empty_cross_batch(),track_count=0),spatial_context)


def _detect_intersections_handle(source,cfg,spatial_context):
    # Eager APIs use the same business subtrack columns and Run identity.
    parent_context = getattr(source, "spatial_context", None)
    if parent_context is not None and parent_context != spatial_context:
        raise V2Error("Refine spatial context does not match Detect spatial context")
    inputs=source.subtracks if hasattr(source,"subtracks") else source
    if isinstance(inputs,dict): tracks=inputs
    else:
        tracks = {}
        for track in inputs:
            if not isinstance(track, RefinementSubtrack):
                raise TypeError("Detect requires RefinementSubtrack columns")
            if track.subtrack_index in tracks:
                raise ValueError("duplicate Run-scoped subtrack identity")
            tracks[track.subtrack_index] = track
    if any(not isinstance(track,RefinementSubtrack) for track in tracks.values()): raise TypeError("Detect requires RefinementSubtrack columns")
    if not tracks:
        return _empty_intersect_result(spatial_context)
    transformer = CoordinateTransformer(spatial_context)
    live = getattr(cfg,"live_status",None)
    own_live = False
    if live is None:
        try:
            from ..telemetry.console_logging import LiveStatus
            live = LiveStatus("Detect: 交叉口检测 / Intersection Detection")
            live.start()
            own_live = True
        except Exception:
            live = None
    tracker = PerfTracker("detect_handle")
    try:
        with tracker.stage("turn_candidates"):
            turns = extract_turn_candidates(iter_track_patches(tracks.items(),cfg.stream_batch_size),cfg,live_status=live,perf=tracker)
        with tracker.stage("cross_track_candidates"):
            crosses = extract_cross_track_candidates(tracks,transformer,cfg,live_status=live,perf=tracker)[0] if cfg.use_cross_track_candidates else empty_cross_batch()
        with tracker.stage("cluster_pipeline"):
            hotspot,clusters = _cluster_pipeline(turns,crosses,transformer,cfg,live_status=live,perf=tracker)
        with tracker.stage("topology"):
            topologies,clusters,observations = _build_topologies_from_tracks(clusters,tracks,cfg,transformer,live_status=live,perf=tracker)
        log_info(tracker.report())
        return IntersectResult(turns,hotspot,clusters,topologies,_build_intersect_report(clusters,topologies,turns,hotspot,crosses,track_count=len(tracks)),spatial_context,observations,crosses)
    finally:
        if own_live and live is not None:
            live.stop()


def _detect_intersections_db(conn,cfg,spatial_context,*,runtime,data_root,refine_reader=None,connect_worker=None):
    if refine_reader is None:
        raise V2Error("formal DB Detect requires a columnar reader")
    if refine_reader.extent() is None:
        return _empty_intersect_result(spatial_context)
    event_store=TurnEventRelation(runtime,data_root); candidate_store=CrossCandidateRelation(runtime,data_root)
    try:
        return _run_columnar_db_detect(cfg,spatial_context,refine_reader,event_store=event_store,candidate_store=candidate_store,conn=conn,refine_id=cfg.refine_id,connect_worker=connect_worker,runtime=runtime,data_root=data_root)
    except BaseException:
        event_store.close(); candidate_store.close(); raise


def _run_columnar_db_detect(cfg,spatial_context,refine_reader,*,runtime,data_root,event_store=None,candidate_store=None,conn=None,refine_id=None,connect_worker=None):
    transformer=CoordinateTransformer(spatial_context)
    if refine_reader.extent() is None: return _empty_intersect_result(spatial_context)
    turn_started = time.perf_counter()
    live = cfg.live_status
    with operation_heartbeat(
        "Detect：转向事件检测",
        emit=(lambda message: live.update(detail=message, elapsed=time.perf_counter()-turn_started)) if live is not None else None,
    ):
        batches = refine_reader.iter_turn_candidates(
            cfg.source_table or cfg.db_table, cfg, batch_size=cfg.stream_batch_size,
        ) if cfg.use_turn_events else ()
        collected = [] if event_store is None else None
        turn_count = 0
        try:
            for batch in batches:
                if event_store is None:
                    collected.append(batch)
                else:
                    event_store.append_batch(batch)
                turn_count += len(batch)
                heartbeat_progress(turn_count, unit="转向事件")
        finally:
            close = getattr(batches, "close", None)
            if close is not None:
                close()
        if event_store is None:
            turns = concatenate_turn_batches(collected)
        else:
            event_store.seal()
            turns = event_store
    crosses=empty_cross_batch()
    if cfg.use_cross_track_candidates:
        if conn is None or refine_id is None or connect_worker is None: raise V2Error("formal DB cross-track requires connection, refine_id, and worker connector")
        crosses=run_grid_cross_track_task(refine_id,refine_reader.extent(),spatial_context,transformer,cfg,connect_worker=connect_worker,grid_cell_size_m=cfg.cross_track_task_grid_m,thread_count=cfg.cross_track_task_threads,max_segments=200_000,max_segment_bytes=268_435_456,max_grid_memberships=2_000_000,max_overflow_pair_space=20_000_000,max_candidates=500_000,max_result_bytes=536_870_912,candidate_store=candidate_store,live_status=cfg.live_status,runtime=runtime,data_root=data_root)
    hotspot,clusters=_cluster_pipeline(turns,crosses,transformer,cfg)
    topologies,clusters,observations=_build_topologies_from_tracks(clusters,{},cfg,transformer,refine_reader=refine_reader)
    return IntersectResult(turns,hotspot,clusters,topologies,_build_intersect_report(clusters,topologies,turns,hotspot,crosses,track_count=refine_reader.track_count()),spatial_context,observations,crosses)

def detect_intersections(
    source: TrajectorySourceHandle | None = None,
    *,
    config: IntersectConfig,
    context: DetectRuntimeContext,
    conn: Any = None,
    connect_worker: Callable[[], Any] | None = None,
    spatial_context: SpatialContext | None = None,
    refine_reader: RefineDatasetReader | None = None,
    runtime: RuntimeConfig,
    data_root: str | Path | None = None,
) -> IntersectResult:
    """Run intersection detection and topology extraction.

    PRD v2 Detect §2

    Two modes:
      - **Handle mode**: ``source`` is a ``TrajectorySourceHandle``.
        All points are read into memory and grouped by vehicle_id.
      - **DB mode**: ``conn`` is a psycopg connection and
        ``config.query_active_from_db`` is True.  A ``RefineDatasetReader``
        supplies columnar turn candidates, cross-track segment pairs and
        complete subtracks for Topology window verification.
    """
    cfg = _IntersectRunConfig(
        **config.as_dict(),
        query_active_from_db=context.query_active_from_db,
        db_table=context.db_table,
        kept_table=context.kept_table,
        source_table=context.source_table,
        refine_id=context.refine_id,
        live_status=context.live_status,
    )
    if conn is not None and cfg.query_active_from_db:
        if cfg.refine_id is None:
            raise V2Error("formal DB Detect requires refine_id")
        refine = fetch_refine_run_row(conn, cfg.refine_id)
        if refine.status != "completed" or refine.source_fingerprint is None:
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
        if refine_reader is None:
            raise V2Error("formal DB Detect requires a RefineDatasetReader")
        descriptor = refine_reader.descriptor
        if descriptor.refine_id is None or str(descriptor.refine_id) != refine.refine_id:
            raise V2Error("Refine artifact does not belong to the parent Refine Run")
        if descriptor.source_fingerprint_json != canonical_json(refine.source_fingerprint):
            raise V2Error("Refine artifact source fingerprint does not match its Run")
        if runtime is None or data_root is None:
            raise V2Error("formal DB Detect requires runtime and data_root")
        return _detect_intersections_db(
            conn, cfg, refine.spatial_context, runtime=runtime, data_root=data_root,
            refine_reader=refine_reader, connect_worker=connect_worker,
        )
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
) -> dict[tuple[int, int], array] | None:
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
    grid: dict[tuple[int, int], array] = {}
    code = index_typecode(len(xs))
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
        indices = grid.get(cell)
        if indices is None:
            indices = grid[cell] = array(code)
        indices.append(index)
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
    grid: dict[tuple[int, int], Sequence[int]],
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
    grid: dict[tuple[int, int], Sequence[int]],
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
    xs: Sequence[float],
    ys: Sequence[float],
    index: int,
    eps_m: float,
    grid: dict[tuple[int, int], array],
    available: ActiveGridLinks,
) -> array:
    """Consume the same exact frontier, ascending globally before LIFO expansion.

    Cell chains are already index-ordered. Merge their filtered streams without
    an all-neighbour Python-int list or a full sort. Every matched node is removed
    while its cell is known; no N-element point-to-cell vector is needed.
    """
    base_x, base_y = xs[index], ys[index]
    eps_sq = eps_m * eps_m
    x_bounds = _dbscan_grid_axis_bounds(base_x, eps_m)
    y_bounds = _dbscan_grid_axis_bounds(base_y, eps_m)
    if x_bounds is None or y_bounds is None:
        raise RuntimeError("DBSCAN grid invariant violated after construction")

    def matching(cell: tuple[int, int]) -> Iterator[int]:
        for candidate in available.iter_cell(cell):
            dx = xs[candidate] - base_x
            dy = ys[candidate] - base_y
            if dx * dx + dy * dy <= eps_sq:
                available.remove(cell, candidate)
                yield candidate

    streams = [
        matching((grid_x, grid_y))
        for grid_x in range(x_bounds[0], x_bounds[1] + 1)
        for grid_y in range(y_bounds[0], y_bounds[1] + 1)
        if available.heads.get((grid_x, grid_y), -1) >= 0
    ]
    return array(available.typecode, heapq.merge(*streams))


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
    # 0: unknown, 1: non-core, 2: core. Preserve all three old states.
    core_flags = bytearray(n)
    available = ActiveGridLinks(grid, n)
    t0 = time.perf_counter()
    last_t = t0

    for i in _perf_iter(perf, range(n), name="dbscan_points", total=n):
        if labels[i] != _UNVISITED:
            continue
        is_core = _grid_has_min_neighbors(
            xs, ys, i, eps_m, min_samples, grid,
        )
        core_flags[i] = 2 if is_core else 1
        if not is_core:
            labels[i] = _NOISE
            continue

        labels[i] = cluster_label
        available.remove((math.floor(xs[i] / eps_m), math.floor(ys[i] / eps_m)), i)
        seeds = _take_grid_frontier_neighbors(
            xs, ys, i, eps_m, grid, available,
        )
        while seeds:
            current = seeds.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_label
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_label
            state = core_flags[current]
            if state == 0:
                is_core = _grid_has_min_neighbors(
                    xs, ys, current, eps_m, min_samples, grid,
                )
                core_flags[current] = 2 if is_core else 1
            else:
                is_core = state == 2
            if is_core:
                if perf is None:
                    seeds.extend(_take_grid_frontier_neighbors(
                        xs, ys, current, eps_m, grid, available,
                    ))
                else:
                    with perf.stage("dbscan_seed_neighbors"):
                        seeds.extend(_take_grid_frontier_neighbors(
                            xs, ys, current, eps_m, grid, available,
                        ))
        cluster_label += 1
        now = time.perf_counter()
        if i % 500 == 0 or (now - last_t) >= 3.0:
            report_detect_progress(
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
            report_detect_progress(
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
