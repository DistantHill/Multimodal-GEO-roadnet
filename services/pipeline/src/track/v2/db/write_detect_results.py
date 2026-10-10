"""Persist Detect runs and their complete result graph atomically."""
from __future__ import annotations

import json
import time
import uuid
import numpy as np
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from ..intersections.detect_intersections import IntersectResult
from ..intersections.relations.source_associations import WriterRelation
from ..models import HotspotCluster, RuntimeConfig, SpatialContext
from ..telemetry.heartbeat import heartbeat_progress, observed_operation
from .point_geometry import point_ewkt, entry_exit_ewkt
from .source_table import quote_table_name
from .insert_batches import (
    _BATCH, _copy_insert, _chunked_template, _chunked_template_returning,
)

_SEQUENCE_TABLES = frozenset({
    "candidate_intersections",
    "candidate_entry_exit_points",
    "intersections",
    "resolved_subtrack_points",
})

@dataclass(frozen=True, slots=True)
class DetectWriteCounts:
    resolved_point_count: int
    tracks_cross_count: int
    turn_event_count: int
    candidate_intersection_count: int
    candidate_entry_exit_point_count: int
    intersection_count: int
    entry_exit_point_count: int
    resolved_sequence_elapsed_s: float = 0.0


@dataclass(frozen=True, slots=True)
class WriterCrossingIDs:
    """Cluster-local calculation identity columns mapped to PostgreSQL IDs."""
    cluster_index: np.ndarray
    local_index: np.ndarray
    persistent_id: np.ndarray

    def resolve(self, cluster_index: int, local_index: int) -> int:
        start=int(np.searchsorted(self.cluster_index,cluster_index,side="left"))
        stop=int(np.searchsorted(self.cluster_index,cluster_index,side="right"))
        position=start+int(np.searchsorted(self.local_index[start:stop],local_index))
        if position>=stop or self.local_index[position]!=local_index:
            raise KeyError((cluster_index,local_index))
        return int(self.persistent_id[position])


def create_detect_run(
    conn: Any,
    *,
    refine_id: str,
    parameters: dict[str, Any],
) -> str:
    detect_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO detect_runs (id, refine_id, parameters, status) "
            "SELECT %s, c.id, %s, 'running' FROM refine_runs AS c "
            "WHERE c.id = %s AND c.status = 'completed' "
            "AND c.processing_srid IS NOT NULL "
            "AND c.projection_config->>'coordinate_semantics_version' = '2' "
            "RETURNING id",
            (detect_id, json.dumps(parameters, ensure_ascii=False), refine_id),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"Refine Run {refine_id} is not a completed projected run"
            )
    return detect_id


def finish_detect_run(conn: Any, detect_id: str, *, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE detect_runs SET status = %s, finished_at = NOW() "
            "WHERE id = %s AND status = 'running'",
            (status, detect_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Detect Run {detect_id} is not running")


@observed_operation("写入 Detect 结果")
def persist_detect_result(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    *,
    runtime: RuntimeConfig,
    data_root: str | Path,
    batch_size: int = _BATCH,
    time_start=None, time_end=None,
) -> DetectWriteCounts:
    """Persist atomically; own narrow local relations until writer completion."""
    with WriterRelation(
        runtime, data_root, detect_id, batch_size=batch_size,
    ) as workspace:
        return _persist_detect_result(conn, detect_id, result, batch_size=batch_size, workspace=workspace,
                                      time_start=time_start, time_end=time_end)


def _persist_detect_result(conn, detect_id, result, *, batch_size, workspace, time_start=None, time_end=None):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refine_id, status FROM detect_runs WHERE id = %s FOR UPDATE",
            (detect_id,),
        )
        run = cur.fetchone()
    if run is None or str(run[1]) != "running":
        raise ValueError(f"Detect Run {detect_id} is not running")
    refine_id = str(run[0])

    ordered_clusters = sorted(
        result.clusters,
        key=lambda cluster: cluster.cluster_calculate_index,
    )
    _require_unique(
        [cluster.cluster_calculate_index for cluster in ordered_clusters],
        "candidate cluster calculation indexes",
    )
    with conn.cursor() as cur:
        candidate_ids = _reserve_ids(cur, "candidate_intersections", len(ordered_clusters))
    candidate_by_calculation = {
        cluster.cluster_calculate_index: candidate_id
        for cluster, candidate_id in zip(ordered_clusters, candidate_ids, strict=True)
    }

    pending_crossings = [(cluster,cluster.crossing_points) for cluster in ordered_clusters if cluster.crossing_points is not None]
    crossing_count=sum(len(batch) for _,batch in pending_crossings)
    cluster_indices=np.concatenate([np.full(len(batch),cluster.cluster_calculate_index,dtype=np.int64) for cluster,batch in pending_crossings]) if pending_crossings else np.empty(0,dtype=np.int64)
    local_indices=np.concatenate([batch.calculation_index for _,batch in pending_crossings]) if pending_crossings else np.empty(0,dtype=np.int64)
    order=np.lexsort((local_indices,cluster_indices))
    sorted_clusters=cluster_indices[order]; sorted_locals=local_indices[order]
    if np.any((sorted_clusters[1:]==sorted_clusters[:-1])&(sorted_locals[1:]==sorted_locals[:-1])): raise ValueError("candidate crossing calculation keys must be unique")
    with conn.cursor() as cur:
        crossing_ids=_reserve_ids(cur,"candidate_entry_exit_points",crossing_count)
    crossing_id_by_calculation=WriterCrossingIDs(sorted_clusters,sorted_locals,np.asarray(crossing_ids,dtype=np.int64)[order])
    crossing_ids_by_cluster={}; offset=0
    for cluster,batch in pending_crossings:
        crossing_ids_by_cluster[cluster.cluster_calculate_index]=np.asarray(crossing_ids[offset:offset+len(batch)],dtype=np.int64)
        offset+=len(batch)

    complete_topologies = sorted(
        (topology for topology in result.topologies if topology.is_complete),
        key=lambda topology: topology.cluster_calculate_index,
    )
    with conn.cursor() as cur:
        intersection_ids = _reserve_ids(cur, "intersections", len(complete_topologies))
    intersection_by_calculation = {
        topology.cluster_calculate_index: intersection_id
        for topology, intersection_id in zip(
            complete_topologies,
            intersection_ids,
            strict=True,
        )
    }
    complete_crossings = [
        (cluster, crossing)
        for cluster, crossing in pending_crossings
        if cluster.cluster_calculate_index in intersection_by_calculation
    ]

    workspace.collect_sources(result, pending_crossings, crossing_ids_by_cluster, candidate_by_calculation)
    resolved_sequence_started = time.perf_counter()
    resolved_point_count = _persist_resolved_sequence(
        conn,
        detect_id,
        refine_id,
        result,
        pending_crossings,
        complete_crossings,
        crossing_id_by_calculation,
        batch_size=batch_size,
        workspace=workspace,
        time_start=time_start, time_end=time_end,
    )
    resolved_sequence_elapsed = time.perf_counter() - resolved_sequence_started

    _insert_candidate_intersections(
        conn,
        detect_id,
        ordered_clusters,
        candidate_ids,
        result.spatial_context,
        batch_size,
    )
    workspace.prepare_annotations(ordered_clusters, candidate_by_calculation, result.hotspot_events)
    tracks_cross_count = _insert_tracks_cross(conn, detect_id, workspace, batch_size=batch_size)
    turn_event_count = _insert_turn_events(conn, detect_id, workspace, batch_size=batch_size)
    _insert_candidate_instances(conn, detect_id, result)
    materialize_turn_event_geom(conn, detect_id)
    materialize_turn_hot_points(conn, detect_id)
    _insert_candidate_crossings(conn, detect_id, workspace, batch_size=batch_size)
    _insert_intersections(
        conn,
        detect_id,
        complete_topologies,
        intersection_ids,
        candidate_by_calculation,
        {cluster.cluster_calculate_index: cluster for cluster in ordered_clusters},
        result.spatial_context,
        batch_size,
    )
    entry_exit_count = _insert_entry_exit_points(
        conn,
        detect_id,
        result,
        intersection_by_calculation,
        crossing_id_by_calculation,
    )
    # Export joins several freshly populated tables by this new Detect UUID.
    # Publish their statistics with the rows instead of waiting for autovacuum,
    # which can otherwise leave the export plan estimating one matching row.
    with conn.cursor() as cur:
        cur.execute(
            "ANALYZE resolved_subtrack_points, candidate_entry_exit_points, "
            "candidate_intersections, intersections, entry_exit_points, "
            "turn_events, tracks_cross, detect_runs, turn_events_geom, "
            "trun_hot_ponits"
        )
    return DetectWriteCounts(
        resolved_point_count=resolved_point_count,
        tracks_cross_count=tracks_cross_count,
        turn_event_count=turn_event_count,
        candidate_intersection_count=len(ordered_clusters),
        candidate_entry_exit_point_count=crossing_count,
        intersection_count=len(complete_topologies),
        entry_exit_point_count=entry_exit_count,
        resolved_sequence_elapsed_s=resolved_sequence_elapsed,
    )


def _iter_refine_member_batches(
    conn: Any,
    refine_id: str,
    batch_size: int,
    *, time_start=None, time_end=None,
) -> Iterator[dict[str, np.ndarray]]:
    """Read one ordered Refine snapshot without libpq/Python materializing it."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    # The caller finishes each COPY before resuming this named cursor. Both
    # operations use the existing atomic write transaction and connection.
    with conn.cursor(name=f"resolved_members_{uuid.uuid4().hex}") as cur:
        query = (
            "SELECT point_kind, point_id, vehicle_id, subtrack_index, anchor_sequence, geom "
            "FROM ("
            " SELECT 'kept'::text AS point_kind, id AS point_id, vehicle_id, "
            "        subtrack_index, sequence_in_subtrack AS anchor_sequence, 0 AS kind_order, geom::text AS geom "
            " FROM kept_trajectory_points WHERE refine_id = %s "
            " UNION ALL "
            " SELECT 'injected', id, vehicle_id, subtrack_index, "
            "        after_sequence_in_subtrack, 1, geom::text "
            " FROM injected_points WHERE refine_id = %s"
            ") AS members "
            "ORDER BY subtrack_index, anchor_sequence, kind_order, point_id"
        )
        params = (refine_id, refine_id)
        if time_start is not None or time_end is not None:
            from .source_table import validate_source_table
            from ..time_selection import time_bounds
            start, end = time_bounds(time_start, time_end)
            with conn.cursor() as metadata:
                metadata.execute("SELECT source_table FROM refine_runs WHERE id=%s", (refine_id,))
                source = validate_source_table(conn, metadata.fetchone()[0])
            lower = start.timestamp() if start else float("-inf")
            upper = end.timestamp() if end else float("inf")
            query = f"""SELECT point_kind, point_id, vehicle_id, subtrack_index, anchor_sequence, geom
                FROM (
                    SELECT 'kept'::text AS point_kind, k.id AS point_id, k.vehicle_id,
                           k.subtrack_index, k.sequence_in_subtrack AS anchor_sequence,
                           0 AS kind_order, k.geom::text AS geom
                    FROM kept_trajectory_points k
                    JOIN {source.quoted_table} s ON s.id=k.source_row_key
                    WHERE k.refine_id=%s AND s.timestamp >= %s AND s.timestamp < %s
                    UNION ALL
                    SELECT 'injected', id, vehicle_id, subtrack_index,
                           after_sequence_in_subtrack, 1, geom::text
                    FROM injected_points
                    WHERE refine_id=%s AND timestamp >= %s AND timestamp < %s
                ) members ORDER BY subtrack_index, anchor_sequence, kind_order, point_id"""
            params = (refine_id, lower, upper, refine_id, lower, upper)
        cur.execute(query, params)
        current_subtrack: int | None = None
        current_vehicle: str | None = None
        sequence = 0
        while raw_rows := cur.fetchmany(batch_size):
            sequences=[]
            for kind,point_id,vehicle,subtrack,_anchor,_geom in raw_rows:
                subtrack=int(subtrack); vehicle=str(vehicle)
                if subtrack!=current_subtrack:
                    current_subtrack,current_vehicle=subtrack,vehicle; sequence=0
                elif vehicle!=current_vehicle: raise ValueError(f"a subtrack contains multiple vehicles: {subtrack}")
                sequences.append(sequence); sequence+=1
            yield {"point_kind":np.asarray([row[0] for row in raw_rows]),"point_id":np.asarray([row[1] for row in raw_rows],dtype=np.int64),"subtrack_index":np.asarray([row[3] for row in raw_rows],dtype=np.int64),"refined_sequence":np.asarray(sequences,dtype=np.int64),"geom":np.asarray([row[5] for row in raw_rows],dtype=object)}


@observed_operation("写入 Detect 解析序列")
def _persist_resolved_sequence(
    conn: Any,
    detect_id: str,
    refine_id: str,
    result: IntersectResult,
    pending_crossings: list[tuple[HotspotCluster, Any]],
    complete_crossings: list[tuple[HotspotCluster, Any]],
    crossing_id_by_calculation: WriterCrossingIDs,
    *,
    batch_size: int,
    workspace: WriterRelation,
    time_start=None, time_end=None,
) -> int:
    """Keep historical p/i/c ordering, streaming exact source keys to disk."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    complete_offsets=[]
    parts={name:[] for name in ("subtrack","progress","placement","direction","cluster","crossing_id")}
    offset=0
    for cluster,batch in complete_crossings:
        if not np.all(batch.refine_point_id_valid): raise ValueError("persisted crossing requires stable segment start")
        complete_offsets.append(offset); offset+=len(batch)
        parts["subtrack"].append(batch.subtrack_index)
        parts["progress"].append(batch.point_index+batch.segment_fraction)
        parts["placement"].append(np.where(batch.segment_fraction>=1.0-1e-12,0,2))
        parts["direction"].append(batch.is_entry.astype(np.int8))
        parts["cluster"].append(np.full(len(batch),cluster.cluster_calculate_index,dtype=np.int64))
        parts["crossing_id"].append(np.fromiter((crossing_id_by_calculation.resolve(cluster.cluster_calculate_index,int(index)) for index in batch.calculation_index),dtype=np.int64,count=len(batch)))
    ordering={name:np.concatenate(values) if values else np.empty(0,dtype=np.float64 if name=="progress" else np.int64) for name,values in parts.items()}
    order=np.lexsort((ordering["crossing_id"],ordering["cluster"],ordering["direction"],ordering["placement"],ordering["progress"],ordering["subtrack"]))
    subtracks,starts=np.unique(ordering["subtrack"][order],return_index=True)
    stops=np.r_[starts[1:],len(order)]
    by_subtrack={int(subtrack):order[start:stop] for subtrack,start,stop in zip(subtracks,starts,stops)}
    crossing_sequences=np.zeros(offset,dtype=np.int64)
    columns = [
        "id", "detect_id", "subtrack_index", "refined_sequence_in_subtrack",
        "sequence_in_subtrack", "kept_point_id", "injected_point_id", "crossing_point_id", "geom",
    ]
    count = 0
    current_subtrack: int | None = None
    current_events=np.empty(0,dtype=np.int64)
    event_index = 0
    sequence = 0

    def finish_subtrack() -> None:
        nonlocal event_index, sequence
        while event_index < len(current_events):
            crossing_sequences[current_events[event_index]] = sequence
            event_index += 1
            sequence += 1

    bounds = {"time_start": time_start, "time_end": time_end}
    batches = _iter_refine_member_batches(
        conn, refine_id, batch_size,
        **(bounds if any(bounds.values()) else {}),
    )
    try:
        for members in batches:
            with conn.cursor() as cur:
                size=len(members["point_id"])
                ids=_reserve_ids(cur,"resolved_subtrack_points",size)
                rows=[]
                for i,resolved_id in enumerate(ids):
                    subtrack=int(members["subtrack_index"][i]); refined_sequence=int(members["refined_sequence"][i])
                    if subtrack!=current_subtrack:
                        finish_subtrack(); current_subtrack=subtrack; current_events=by_subtrack.pop(subtrack,np.empty(0,dtype=np.int64)); event_index=sequence=0
                    point_key=(float(refined_sequence),1,-1,-1,resolved_id)
                    def before_point(index):
                        return (ordering["progress"][index],ordering["placement"][index],ordering["direction"][index],ordering["cluster"][index],ordering["crossing_id"][index])<point_key
                    while event_index<len(current_events) and before_point(current_events[event_index]):
                        crossing_sequences[current_events[event_index]]=sequence; event_index+=1; sequence+=1
                    kept=members["point_kind"][i]=="kept"; point_id=int(members["point_id"][i])
                    rows.append((resolved_id,detect_id,subtrack,refined_sequence,sequence,point_id if kept else None,point_id if not kept else None,None,members["geom"][i])); sequence+=1
                workspace.add_mappings(members["point_kind"],members["point_id"],members["subtrack_index"],np.asarray(ids,dtype=np.int64))
                _copy_insert(cur, "resolved_subtrack_points", columns, rows, observe=False)
                count += len(rows)
                heartbeat_progress(count)
        finish_subtrack()
    finally:
        batches.close()
    workspace.resolve_sources()
    if by_subtrack:
        raise ValueError("a persisted crossing references a missing Refine subtrack")
    # Allocate c IDs after every p/i ID, retaining resolved sequence semantics.
    for (cluster,batch),offset in zip(complete_crossings,complete_offsets):
        for start in range(0,len(batch),batch_size):
            stop=min(start+batch_size,len(batch))
            with conn.cursor() as cur:
                ids=_reserve_ids(cur,"resolved_subtrack_points",stop-start)
                rows=[(resolved_id,detect_id,int(batch.subtrack_index[i]),None,crossing_sequences[offset+i],None,None,crossing_id_by_calculation.resolve(cluster.cluster_calculate_index,int(batch.calculation_index[i])),point_ewkt(batch.lonlat[i])) for i,resolved_id in zip(range(start,stop),ids)]
                _copy_insert(cur,"resolved_subtrack_points",columns,rows,observe=False); count+=len(rows); heartbeat_progress(count)
    return count


def _insert_candidate_intersections(
    conn: Any,
    detect_id: str,
    clusters: list[HotspotCluster],
    ids: tuple[int, ...],
    spatial_context: SpatialContext,
    batch_size: int,
) -> None:
    contributions: list[dict[str, dict[str, float | int]]] = []
    for cluster in clusters:
        total = sum(cluster.source_counts.values())
        contributions.append({
            name: {
                "count": count,
                "ratio": (count / total if total else 0.0),
            }
            for name, count in sorted(cluster.source_counts.items())
        })
    rows = [(
        candidate_id,
        detect_id,
        cluster.centroid_x,
        cluster.centroid_y,
        cluster.centroid_lon,
        cluster.centroid_lat,
        cluster.radius_m,
        json.dumps(contribution, ensure_ascii=False),
        json.dumps(list(cluster.critical_sources), ensure_ascii=False),
        cluster.centroid_x,
        cluster.centroid_y,
        spatial_context.processing_srid,
        cluster.radius_m,
    ) for cluster, candidate_id, contribution in zip(
        clusters, ids, contributions, strict=True
    )]
    with conn.cursor() as cur:
        _chunked_template(
            cur,
            "candidate_intersections",
            [
                "id", "detect_id", "centroid_x", "centroid_y",
                "centroid_lon", "centroid_lat", "radius_m", "contributors",
                "critical_contributors", "geom",
            ],
            rows,
            ", ".join(
                ["%s"] * 9
                + [
                    "ST_Transform(ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), "
                    "%s::integer), %s), 4326)"
                ]
            ),
            batch_size=batch_size,
        )


def _copy_workspace_rows(conn, table, columns, rows, *, total, batch_size):
    """Keep one COPY batch and one iterator; caller owns transaction rollback."""
    written = 0
    batch = []
    with closing(rows):
        with conn.cursor() as cur:
            for row in rows:
                batch.append(row)
                if len(batch) == batch_size:
                    _copy_insert(cur, table, columns, batch, observe=False)
                    written += len(batch)
                    heartbeat_progress(written, total=total)
                    batch.clear()
            if batch:
                _copy_insert(cur, table, columns, batch, observe=False)
                written += len(batch)
                heartbeat_progress(written, total=total)
    return written


@observed_operation("写入跨轨迹事件")
def _insert_tracks_cross(conn, detect_id, workspace, *, batch_size):
    workspace.validate_cross_pairs(detect_id)
    return _copy_workspace_rows(
        conn, "tracks_cross",
        ["detect_id", "candidate_intersection_id", "from_segment_start_point_id",
         "to_segment_start_point_id", "x", "y", "lon", "lat",
         "algorithm_id", "instance_id", "local_candidate_id", "evidence_index"],
        workspace.with_instance_identity("cross", workspace.iter_validated_cross_rows()),
        total=workspace.counts[1], batch_size=batch_size,
    )


@observed_operation("写入转向事件")
def _insert_turn_events(conn, detect_id, workspace, *, batch_size):
    return _copy_workspace_rows(
        conn, "turn_events",
        ["detect_id", "candidate_intersection_id", "resolved_point_id", "entry_bearing",
         "exit_bearing", "turn_deg", "score", "g_star_z",
         "algorithm_id", "instance_id", "local_candidate_id", "evidence_index"],
        workspace.with_instance_identity("turn", workspace.iter_rows(0, detect_id)), total=workspace.counts[0], batch_size=batch_size,
    )


def _insert_candidate_instances(conn, detect_id, result):
    from psycopg.types.json import Jsonb
    for item in getattr(result, "candidate_results", ()):
        evidence = item.events if item.events is not None else item.candidates
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO detect_candidate_instances "
                "(detect_id,algorithm_id,instance_id,params,evidence_kind,evidence_offset,evidence_count,candidate_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (detect_id, item.algorithm_id, item.instance_id, Jsonb(asdict(item.params)),
                 item.evidence_kind, item.evidence_offset, len(evidence), len(item.candidates)),
            )


@observed_operation("物化转向事件几何")
def materialize_turn_event_geom(conn: Any, detect_id: str) -> int:
    """Copy this Detect Run's events and geometry in the caller's transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.source_table FROM detect_runs d "
            "JOIN refine_runs r ON r.id = d.refine_id WHERE d.id = %s",
            (detect_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown Detect Run: {detect_id}")
        source = quote_table_name(str(row[0]))
        cur.execute(
            f"""
            INSERT INTO turn_events_geom (
                id, detect_id, candidate_intersection_id, resolved_point_id,
                entry_bearing, exit_bearing, turn_deg, score, g_star_z,
                created_at, geom, algorithm_id, instance_id, local_candidate_id, evidence_index
            )
            SELECT te.id, te.detect_id, te.candidate_intersection_id,
                   te.resolved_point_id, te.entry_bearing, te.exit_bearing,
                   te.turn_deg, te.score, te.g_star_z, te.created_at,
                   COALESCE(tp.geom, ip.geom), te.algorithm_id, te.instance_id, te.local_candidate_id, te.evidence_index
            FROM turn_events te
            LEFT JOIN resolved_subtrack_points rp
              -- The composite event FK already enforces matching Detect IDs.
              -- Joining by the global PK avoids a detect-only scan with stale
              -- statistics immediately after COPY inserts a new Run.
              ON rp.id = te.resolved_point_id
            LEFT JOIN kept_trajectory_points kp ON kp.id = rp.kept_point_id
            LEFT JOIN {source} tp ON tp.id = kp.source_row_key
            LEFT JOIN injected_points ip ON ip.id = rp.injected_point_id
            WHERE te.detect_id = %s
            """,
            (detect_id,),
        )
        return int(cur.rowcount)


@observed_operation("物化转向热点")
def materialize_turn_hot_points(conn: Any, detect_id: str) -> int:
    """Persist G* selected DBSCAN inputs, including unclustered noise.

    Writer annotations assign g_star_z only to result.hotspot_events.
    Candidate membership is deliberately independent of hotspot selection.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trun_hot_ponits (
                id, detect_id, candidate_intersection_id, resolved_point_id,
                entry_bearing, exit_bearing, turn_deg, score, g_star_z,
                created_at, geom, algorithm_id, instance_id, local_candidate_id, evidence_index
            )
            SELECT id, detect_id, candidate_intersection_id, resolved_point_id,
                   entry_bearing, exit_bearing, turn_deg, score, g_star_z,
                   created_at, geom, algorithm_id, instance_id, local_candidate_id, evidence_index
            FROM turn_events_geom
            WHERE detect_id = %s AND g_star_z IS NOT NULL
            """,
            (detect_id,),
        )
        return int(cur.rowcount)


@observed_operation("写入候选穿越点")
def _insert_candidate_crossings(conn, detect_id, workspace, *, batch_size):
    return _copy_workspace_rows(
        conn, "candidate_entry_exit_points",
        ["id", "detect_id", "candidate_intersection_id", "segment_start_point_id",
         "crossing_type", "crossing_x", "crossing_y", "crossing_lon", "crossing_lat"],
        workspace.iter_rows(2, detect_id), total=workspace.counts[2], batch_size=batch_size,
    )


def _insert_intersections(
    conn: Any,
    detect_id: str,
    topologies: list[Any],
    ids: tuple[int, ...],
    candidate_ids: dict[int, int],
    clusters: dict[int, HotspotCluster],
    spatial_context: SpatialContext,
    batch_size: int,
) -> None:
    rows: list[tuple[Any, ...]] = []
    for topology, intersection_id in zip(topologies, ids, strict=True):
        cluster = clusters[topology.cluster_calculate_index]
        radius = topology.clip_radius_m or cluster.radius_m
        if radius <= 0:
            raise ValueError("a complete intersection requires a positive clip radius")
        rows.append((
            intersection_id,
            detect_id,
            candidate_ids[topology.cluster_calculate_index],
            json.dumps(topology.branch_centers_deg, ensure_ascii=False),
            json.dumps({
                f"{key[0]},{key[1]}": value
                for key, value in topology.transition_counts.items()
            }, ensure_ascii=False),
            topology.observation_count,
            radius,
            cluster.centroid_x,
            cluster.centroid_y,
            spatial_context.processing_srid,
            radius,
        ))
    with conn.cursor() as cur:
        _chunked_template(
            cur,
            "intersections",
            [
                "id", "detect_id", "candidate_intersection_id",
                "branch_centers_deg", "transition_counts", "observation_count",
                "clip_radius_m", "geom",
            ],
            rows,
            ", ".join(
                ["%s"] * 7
                + [
                    "ST_Transform(ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), "
                    "%s::integer), %s), 4326)"
                ]
            ),
            batch_size=batch_size,
        )


def _insert_entry_exit_points(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    intersection_ids: dict[int, int],
    crossing_ids: WriterCrossingIDs,
) -> int:
    rows=[]
    crossings={cluster.cluster_calculate_index:cluster.crossing_points for cluster in result.clusters}
    for observations in result.observations:
        cluster_index=observations.cluster_calculate_index
        intersection_id=intersection_ids.get(cluster_index)
        if intersection_id is None: continue
        batch=crossings.get(cluster_index)
        if batch is None: raise ValueError("observation references unknown crossing batch")
        crossing_order=np.argsort(batch.calculation_index)
        local_indices=batch.calculation_index[crossing_order]
        for i in range(len(observations)):
            if not (observations.entry_crossing_calculate_index_valid[i] and observations.exit_crossing_calculate_index_valid[i]): continue
            entry_key=(cluster_index,int(observations.entry_crossing_calculate_index[i])); exit_key=(cluster_index,int(observations.exit_crossing_calculate_index[i]))
            positions=np.searchsorted(local_indices,np.asarray([entry_key[1],exit_key[1]],dtype=np.int64))
            if np.any(positions>=len(local_indices)) or not np.array_equal(local_indices[positions],np.asarray([entry_key[1],exit_key[1]],dtype=np.int64)): raise ValueError("observation references unknown crossing")
            a,b=crossing_order[positions]
            if not batch.is_entry[a] or batch.is_entry[b]: raise ValueError("observation types must be entry/exit")
            if batch.subtrack_index[a]!=batch.subtrack_index[b] or batch.vehicle_id[a]!=batch.vehicle_id[b]: raise ValueError("observation crossings must share one subtrack")
            if batch.point_index[a]+batch.segment_fraction[a]>=batch.point_index[b]+batch.segment_fraction[b]: raise ValueError("entry crossing must precede exit")
            rows.append((detect_id,intersection_id,float(observations.entry_bearing[i]),float(observations.exit_bearing[i]),crossing_ids.resolve(*entry_key),crossing_ids.resolve(*exit_key),entry_exit_ewkt(batch.lonlat[a],batch.lonlat[b])))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "entry_exit_points",
            [
                "detect_id", "intersection_id", "entry_bearing", "exit_bearing",
                "entry_crossing_id", "exit_crossing_id", "geom",
            ],
            rows,
        )
    return len(rows)


def _assign_unique(mapping: dict[int, int], key: int, value: int) -> None:
    previous = mapping.get(key)
    if previous is not None and previous != value:
        raise ValueError(f"Detect entity {key} belongs to multiple candidates")
    mapping[key] = value


def _require_unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _reserve_ids(cur: Any, table: str, count: int) -> tuple[int, ...]:
    if table not in _SEQUENCE_TABLES:
        raise ValueError(f"unsupported sequence-backed table: {table}")
    if count <= 0:
        return ()
    cur.execute(
        "SELECT nextval(pg_get_serial_sequence(%s, 'id')::regclass) "
        "FROM generate_series(1, %s)",
        (table, count),
    )
    ids = tuple(int(row[0]) for row in cur.fetchall())
    if len(ids) != count or len(set(ids)) != count:
        raise RuntimeError(f"failed to reserve {count} IDs for {table}")
    return ids
