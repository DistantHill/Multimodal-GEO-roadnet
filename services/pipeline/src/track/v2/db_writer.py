"""Schema-v3 PostgreSQL writers.

Run lifecycle commits remain the caller's responsibility. In particular,
``persist_detect_result`` writes the complete resolved sequence and every
Detect child artifact without committing, so the caller can atomically mark
the Detect Run completed or roll everything back.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import nullcontext, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .writer_relation import WriterRelation
from .heartbeat import observed_operation, heartbeat_progress, operation_heartbeat
from .db.source_table import quote_table_name
from .detect import IntersectResult
from .models import (
    CenterlineFitBundle,
    RefinedPointRef,
    ClipConfig,
    ClipResult,
    ExcludedIndex,
    FittingConfig,
    GeneratedReason,
    HotspotCluster,
    RefinementSubtrack,
    RuntimeConfig,
    SpatialContext,
    TrajectoryPoint,
)
from .projection import CoordinateTransformer, spatial_context_from_values


_BATCH = 5_000
_SEQUENCE_TABLES = frozenset({
    "candidate_intersections",
    "candidate_entry_exit_points",
    "intersections",
    "resolved_subtrack_points",
})


@dataclass(frozen=True, slots=True)
class RefineRunRecord:
    id: str
    source_table: str
    parameters: dict[str, Any]
    status: str
    spatial_context: SpatialContext
    source_fingerprint: dict[str, object] | None = None


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
class _RefineMember:
    identity: RefinedPointRef
    vehicle_id: str


def create_refine_run(
    conn: Any,
    *,
    source_table: str,
    parameters: dict[str, Any],
    spatial_context: SpatialContext,
) -> str:
    CoordinateTransformer(spatial_context)
    refine_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refine_runs "
            "(id, source_table, source_srid, processing_srid, "
            "projection_config, parameters, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'running')",
            (
                refine_id,
                source_table,
                spatial_context.source_srid,
                spatial_context.processing_srid,
                json.dumps(spatial_context.projection_config, ensure_ascii=False),
                json.dumps(parameters, ensure_ascii=False),
            ),
        )
    return refine_id


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
            "AND c.source_fingerprint IS NOT NULL "
            "AND c.processing_srid IS NOT NULL "
            "AND c.projection_config->>'coordinate_semantics_version' = '2' "
            "RETURNING id",
            (detect_id, json.dumps(parameters, ensure_ascii=False), refine_id),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"Refine Run {refine_id} is not a completed immutable-source run"
            )
    return detect_id


def create_fit_run(
    conn: Any,
    *,
    detect_id: str,
    clip_config: ClipConfig,
    fitting_config: FittingConfig,
    parameters: dict[str, Any],
) -> str:
    fit_id = str(uuid.uuid4())
    parameters = {
        **parameters,
        "clip": clip_config.as_dict(),
        "fitting": fitting_config.as_dict(),
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fit_runs "
            "(id, detect_id, clip_algorithm, clip_version, fitting_algorithm, "
            "fitting_version, parameters, status) "
            "SELECT %s, id, 'resolved_active_set', 'v3', "
            "'normalized_arc_coordinate_median', 'v3', %s, 'running' "
            "FROM detect_runs WHERE id = %s AND status = 'completed' RETURNING id",
            (fit_id, json.dumps(parameters, ensure_ascii=False), detect_id),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Detect Run {detect_id} is not completed")
    return fit_id


def finish_refine_run(
    conn: Any,
    refine_id: str,
    *,
    status: str,
    source_fingerprint: dict[str, object] | None = None,
) -> None:
    if status == "completed" and source_fingerprint is None:
        raise ValueError("a completed Refine Run requires source_fingerprint")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE refine_runs SET status = %s, source_fingerprint = %s, "
            "finished_at = NOW() WHERE id = %s AND status = 'running'",
            (
                status,
                (
                    json.dumps(source_fingerprint, ensure_ascii=False)
                    if source_fingerprint is not None
                    else None
                ),
                refine_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Refine Run {refine_id} is not running")


def finish_detect_run(conn: Any, detect_id: str, *, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE detect_runs SET status = %s, finished_at = NOW() "
            "WHERE id = %s AND status = 'running'",
            (status, detect_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Detect Run {detect_id} is not running")


def finish_fit_run(
    conn: Any,
    fit_id: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fit_runs SET status = %s, report = %s, finished_at = NOW() "
            "WHERE id = %s AND status = 'running'",
            (
                status,
                json.dumps(report, ensure_ascii=False) if report is not None else None,
                fit_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Fit Run {fit_id} is not running")


def fetch_refine_run(conn: Any, refine_id: str) -> RefineRunRecord:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_table, parameters, status, source_srid, "
            "processing_srid, projection_config, source_fingerprint "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Refine Run: {refine_id}")
    parameters = _json_object(row[2], "Refine Run parameters")
    fingerprint = (
        None if row[7] is None else _json_object(row[7], "source fingerprint")
    )
    return RefineRunRecord(
        id=str(row[0]),
        source_table=str(row[1]),
        parameters=parameters,
        status=str(row[3]),
        spatial_context=spatial_context_from_values(row[4], row[5], row[6]),
        source_fingerprint=fingerprint,
    )


def insert_trajectory_points(
    conn: Any,
    points: list[TrajectoryPoint],
    *,
    table: str = "trajectory_points",
    batch_size: int,
) -> dict[tuple[str, str], int]:
    """Standalone CSV importer helper; Pipeline itself is table-only in v3."""
    if not points:
        return {}
    rows = [(
        point.vehicle_id,
        point.timestamp,
        point.lon,
        point.lat,
        point.heading,
        point.speed,
        json.dumps(point.extra, ensure_ascii=False),
    ) for point in points]
    with conn.cursor() as cur:
        returned = _chunked_insert_returning(
            cur,
            quote_table_name(table),
            ["vehicle_id", "timestamp", "lon", "lat", "heading", "speed", "extra"],
            rows,
            ["id"],
            batch_size=batch_size,
        )
    return {
        (point.point_ref.source_id, point.point_ref.source_row_key): int(row[0])
        for point, row in zip(points, returned, strict=True)
    }


def insert_excluded_points(
    conn: Any,
    excluded: ExcludedIndex,
    *,
    refine_id: str,
    batch_size: int = _BATCH,
) -> int:
    rows = [
        (refine_id, int(item.point_ref.source_row_key), item.reason)
        for item in excluded.items
    ]
    with conn.cursor() as cur:
        _chunked_insert(
            cur,
            "excluded_points",
            ["refine_id", "source_row_key", "reason"],
            rows,
            batch_size=batch_size,
        )
    return len(rows)


def insert_kept_trajectory_points(
    conn: Any,
    subtracks: list[RefinementSubtrack],
    *,
    refine_id: str,
) -> int:
    rows: list[tuple[Any, ...]] = []
    for subtrack in subtracks:
        sequence = 0
        for point in subtrack.points:
            if point.point_ref.generated_reason:
                continue
            rows.append((
                refine_id,
                int(point.point_ref.source_row_key),
                point.vehicle_id,
                subtrack.subtrack_index,
                sequence,
            ))
            sequence += 1
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "kept_trajectory_points",
            [
                "refine_id",
                "source_row_key",
                "vehicle_id",
                "subtrack_index",
                "sequence_in_subtrack",
            ],
            rows,
        )
    return len(rows)


def insert_injected_points(
    conn: Any,
    subtracks: list[RefinementSubtrack],
    *,
    refine_id: str,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> int:
    injected: list[tuple[RefinementSubtrack, int, TrajectoryPoint]] = []
    for subtrack in subtracks:
        kept_sequence = -1
        for point in subtrack.points:
            if point.point_ref.generated_reason != GeneratedReason.HEADING_ORTHOGONAL:
                kept_sequence += 1
                continue
            if kept_sequence < 0:
                raise ValueError("an injected point requires a preceding kept point")
            injected.append((subtrack, kept_sequence, point))
    transformer = CoordinateTransformer(spatial_context)
    xy = transformer.forward_many((point.lon, point.lat) for _, _, point in injected)
    rows = [(
        refine_id,
        point.vehicle_id,
        subtrack.subtrack_index,
        anchor,
        point.timestamp,
        point.lon,
        point.lat,
        x,
        y,
        point.heading,
        point.speed,
        str(point.point_ref.generated_reason),
    ) for (subtrack, anchor, point), (x, y) in zip(injected, xy, strict=True)]
    with conn.cursor() as cur:
        _chunked_insert(
            cur,
            "injected_points",
            [
                "refine_id",
                "vehicle_id",
                "subtrack_index",
                "after_sequence_in_subtrack",
                "timestamp",
                "lon",
                "lat",
                "x",
                "y",
                "heading",
                "speed",
                "generated_reason",
            ],
            rows,
            batch_size=batch_size,
        )
    return len(rows)


@observed_operation("写入 Detect 结果")
def persist_detect_result(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    *,
    runtime: RuntimeConfig,
    data_root: str | Path,
    batch_size: int = _BATCH,
) -> DetectWriteCounts:
    """Persist atomically; own narrow local relations until writer completion."""
    with WriterRelation(
        runtime, data_root, detect_id, batch_size=batch_size,
    ) as workspace:
        return _persist_detect_result(conn, detect_id, result, batch_size=batch_size, workspace=workspace)


def _persist_detect_result(conn, detect_id, result, *, batch_size, workspace):
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

    pending_crossings: list[tuple[HotspotCluster, Any]] = [
        (cluster, crossing)
        for cluster in ordered_clusters
        for crossing in cluster.crossing_points
    ]
    crossing_calculation_keys = [
        (cluster.cluster_calculate_index, crossing.crossing_calculate_index)
        for cluster, crossing in pending_crossings
    ]
    _require_unique(crossing_calculation_keys, "candidate crossing calculation keys")
    with conn.cursor() as cur:
        crossing_ids = _reserve_ids(
            cur,
            "candidate_entry_exit_points",
            len(pending_crossings),
        )
    crossing_id_by_calculation = dict(
        zip(crossing_calculation_keys, crossing_ids, strict=True)
    )

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

    workspace.collect_sources(result, pending_crossings, crossing_ids, candidate_by_calculation)
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
    return DetectWriteCounts(
        resolved_point_count=resolved_point_count,
        tracks_cross_count=tracks_cross_count,
        turn_event_count=turn_event_count,
        candidate_intersection_count=len(ordered_clusters),
        candidate_entry_exit_point_count=len(pending_crossings),
        intersection_count=len(complete_topologies),
        entry_exit_point_count=entry_exit_count,
        resolved_sequence_elapsed_s=resolved_sequence_elapsed,
    )


@observed_operation("写入中心线与来源片段")
def insert_centerline_bundle(
    conn: Any,
    bundle: CenterlineFitBundle,
    clips: ClipResult,
    *,
    fit_id: str,
    detect_id: str,
) -> tuple[int, ...]:
    if clips.detect_id is not None and clips.detect_id != detect_id:
        raise ValueError("ClipResult belongs to a different Detect Run")
    centerlines = [
        centerline
        for fit_result in (bundle.combined, bundle.a_to_b, bundle.b_to_a)
        for centerline in fit_result.centerlines
    ]
    rows = [(
        fit_id,
        detect_id,
        centerline.variant,
        centerline.from_intersection_id,
        centerline.to_intersection_id,
        f"LINESTRING({', '.join(f'{lon} {lat}' for lon, lat in centerline.points)})",
        centerline.length_m,
        centerline.forward_clip_count,
        centerline.reverse_clip_count,
    ) for centerline in centerlines]
    with conn.cursor() as cur:
        returned = _chunked_template_returning(
            cur,
            "centerlines",
            [
                "fit_id", "detect_id", "variant", "from_intersection_id",
                "to_intersection_id", "geom", "length_m",
                "forward_clip_count", "reverse_clip_count",
            ],
            rows,
            ", ".join(["%s"] * 5 + ["ST_GeomFromText(%s, 4326)"] + ["%s"] * 3),
            ["id", "variant", "from_intersection_id", "to_intersection_id"],
        )
    id_by_key = {
        (str(variant), int(from_id), int(to_id)): int(centerline_id)
        for centerline_id, variant, from_id, to_id in returned
    }
    expected_keys = {
        (
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )
        for centerline in centerlines
    }
    if id_by_key.keys() != expected_keys:
        missing = sorted(expected_keys - id_by_key.keys())
        unexpected = sorted(id_by_key.keys() - expected_keys)
        raise RuntimeError(
            "centerline insert returned an incomplete id mapping: "
            f"missing={missing}, unexpected={unexpected}"
        )
    ordered_ids = tuple(
        id_by_key[(
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )]
        for centerline in centerlines
    )
    get_clip = getattr(clips.clips, "get_by_clip_id", None)
    if not callable(get_clip):
        clips_by_id = {clip.clip_id: clip for clip in clips.clips}
        get_clip = clips_by_id.__getitem__
    manifest_rows: list[tuple[Any, ...]] = []
    for centerline_id, centerline in zip(ordered_ids, centerlines, strict=True):
        for ordinal, clip_id in enumerate(centerline.source_clip_ids):
            clip = get_clip(clip_id)
            if clip.clip_type != "between_intersection":
                raise ValueError("only between_intersection clips may back a centerline")
            if clip.start.crossing_id is None or clip.end.crossing_id is None:
                raise ValueError("centerline source clips require crossing IDs")
            manifest_rows.append((
                centerline_id,
                detect_id,
                ordinal,
                clip.start.crossing_id,
                clip.end.crossing_id,
                clip.start.intersection_id != centerline.from_intersection_id,
            ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "centerline_source_clips",
            [
                "centerline_id", "detect_id", "input_ordinal",
                "start_crossing_id", "end_crossing_id",
                "reverse_to_centerline_direction",
            ],
            manifest_rows,
        )
    return ordered_ids


def _iter_refine_member_batches(
    conn: Any,
    refine_id: str,
    batch_size: int,
) -> Iterator[list[_RefineMember]]:
    """Read one ordered Refine snapshot without libpq/Python materializing it."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    # The caller finishes each COPY before resuming this named cursor. Both
    # operations use the existing atomic write transaction and connection.
    with conn.cursor(name=f"resolved_members_{uuid.uuid4().hex}") as cur:
        cur.execute(
            "SELECT point_kind, point_id, vehicle_id, subtrack_index, anchor_sequence "
            "FROM ("
            " SELECT 'kept'::text AS point_kind, id AS point_id, vehicle_id, "
            "        subtrack_index, sequence_in_subtrack AS anchor_sequence, 0 AS kind_order "
            " FROM kept_trajectory_points WHERE refine_id = %s "
            " UNION ALL "
            " SELECT 'injected', id, vehicle_id, subtrack_index, "
            "        after_sequence_in_subtrack, 1 "
            " FROM injected_points WHERE refine_id = %s"
            ") AS members "
            "ORDER BY subtrack_index, anchor_sequence, kind_order, point_id",
            (refine_id, refine_id),
        )
        current_subtrack: int | None = None
        current_vehicle: str | None = None
        sequence = 0
        while raw_rows := cur.fetchmany(batch_size):
            members: list[_RefineMember] = []
            for kind, point_id, vehicle, subtrack, _anchor in raw_rows:
                subtrack = int(subtrack)
                vehicle = str(vehicle)
                if subtrack != current_subtrack:
                    current_subtrack, current_vehicle = subtrack, vehicle
                    sequence = 0
                elif vehicle != current_vehicle:
                    raise ValueError(f"a subtrack contains multiple vehicles: {subtrack}")
                members.append(_RefineMember(
                    identity=RefinedPointRef(
                        point_kind=str(kind),  # type: ignore[arg-type]
                        point_id=int(point_id),
                        subtrack_index=subtrack,
                        refined_sequence_in_subtrack=sequence,
                    ),
                    vehicle_id=vehicle,
                ))
                sequence += 1
            yield members


@observed_operation("写入 Detect 解析序列")
def _persist_resolved_sequence(
    conn: Any,
    detect_id: str,
    refine_id: str,
    result: IntersectResult,
    pending_crossings: list[tuple[HotspotCluster, Any]],
    complete_crossings: list[tuple[HotspotCluster, Any]],
    crossing_id_by_calculation: dict[tuple[int, int], int],
    *,
    batch_size: int,
    workspace: WriterRelation,
) -> int:
    """Keep historical p/i/c ordering, streaming exact source keys to disk."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    # Only c events, already present in the Detect result, need random access.
    by_subtrack: dict[int, list[tuple[tuple[Any, ...], int]]] = {}
    for ordinal, (cluster, crossing) in enumerate(complete_crossings):
        identity = crossing.segment_start
        if identity is None:
            raise ValueError("a persisted crossing requires a stable segment start")
        crossing_id = crossing_id_by_calculation[
            (cluster.cluster_calculate_index, crossing.crossing_calculate_index)
        ]
        by_subtrack.setdefault(identity.subtrack_index, []).append((
            (
                identity.refined_sequence_in_subtrack + crossing.segment_fraction,
                0 if crossing.segment_fraction >= 1.0 - 1e-12 else 2,
                0 if not crossing.is_entry else 1,
                cluster.cluster_calculate_index,
                crossing_id,
            ),
            ordinal,
        ))
    for events in by_subtrack.values():
        events.sort(key=lambda item: item[0])
    crossing_sequences: dict[int, int] = {}
    columns = [
        "id", "detect_id", "subtrack_index", "refined_sequence_in_subtrack",
        "sequence_in_subtrack", "kept_point_id", "injected_point_id", "crossing_point_id",
    ]
    count = 0
    current_subtrack: int | None = None
    current_events: list[tuple[tuple[Any, ...], int]] = []
    event_index = 0
    sequence = 0

    def finish_subtrack() -> None:
        nonlocal event_index, sequence
        while event_index < len(current_events):
            crossing_sequences[current_events[event_index][1]] = sequence
            event_index += 1
            sequence += 1

    batches = _iter_refine_member_batches(conn, refine_id, batch_size)
    try:
        for members in batches:
            with conn.cursor() as cur:
                ids = _reserve_ids(cur, "resolved_subtrack_points", len(members))
                rows: list[tuple[Any, ...]] = []
                for member, resolved_id in zip(members, ids, strict=True):
                    identity = member.identity
                    if identity.subtrack_index != current_subtrack:
                        finish_subtrack()
                        current_subtrack = identity.subtrack_index
                        current_events = by_subtrack.pop(current_subtrack, [])
                        event_index = sequence = 0
                    point_key = (
                        float(identity.refined_sequence_in_subtrack), 1, -1, -1, resolved_id,
                    )
                    while (
                        event_index < len(current_events)
                        and current_events[event_index][0] < point_key
                    ):
                        crossing_sequences[current_events[event_index][1]] = sequence
                        event_index += 1
                        sequence += 1
                    rows.append((
                        resolved_id, detect_id, identity.subtrack_index,
                        identity.refined_sequence_in_subtrack, sequence,
                        identity.point_id if identity.point_kind == "kept" else None,
                        identity.point_id if identity.point_kind == "injected" else None,
                        None,
                    ))
                    sequence += 1
                workspace.add_mappings(
                    (member.identity, resolved_id)
                    for member, resolved_id in zip(members, ids, strict=True)
                )
                _copy_insert(cur, "resolved_subtrack_points", columns, rows, observe=False)
                count += len(rows)
                heartbeat_progress(count)
        finish_subtrack()
    finally:
        batches.close()
    workspace.resolve_sources()
    if by_subtrack:
        raise ValueError("a persisted crossing references a missing Refine subtrack")
    # Allocate c IDs only after every p/i ID, as in the original writer.
    for start in range(0, len(complete_crossings), batch_size):
        crossings = complete_crossings[start:start + batch_size]
        with conn.cursor() as cur:
            ids = _reserve_ids(cur, "resolved_subtrack_points", len(crossings))
            rows = []
            for ordinal, ((cluster, crossing), resolved_id) in enumerate(
                zip(crossings, ids, strict=True), start,
            ):
                rows.append((
                    resolved_id, detect_id, crossing.segment_start.subtrack_index,
                    None, crossing_sequences[ordinal], None, None,
                    crossing_id_by_calculation[
                        (cluster.cluster_calculate_index, crossing.crossing_calculate_index)
                    ],
                ))
            _copy_insert(cur, "resolved_subtrack_points", columns, rows, observe=False)
            count += len(rows)
            heartbeat_progress(count)
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
         "to_segment_start_point_id", "x", "y", "lon", "lat"],
        workspace.iter_validated_cross_rows(),
        total=workspace.counts[1], batch_size=batch_size,
    )


@observed_operation("写入转向事件")
def _insert_turn_events(conn, detect_id, workspace, *, batch_size):
    return _copy_workspace_rows(
        conn, "turn_events",
        ["detect_id", "candidate_intersection_id", "resolved_point_id", "entry_bearing",
         "exit_bearing", "turn_deg", "score", "g_star_z"],
        workspace.iter_rows(0, detect_id), total=workspace.counts[0], batch_size=batch_size,
    )


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
    crossing_ids: dict[tuple[int, int], int],
) -> int:
    rows: list[tuple[Any, ...]] = []
    crossings = {
        (cluster.cluster_calculate_index, crossing.crossing_calculate_index): crossing
        for cluster in result.clusters
        for crossing in cluster.crossing_points
    }
    for observation in result.observations:
        intersection_id = intersection_ids.get(observation.cluster_calculate_index)
        if intersection_id is None:
            continue
        if (
            observation.entry_crossing_calculate_index is None
            or observation.exit_crossing_calculate_index is None
        ):
            continue
        entry_key = (
            observation.cluster_calculate_index,
            observation.entry_crossing_calculate_index,
        )
        exit_key = (
            observation.cluster_calculate_index,
            observation.exit_crossing_calculate_index,
        )
        entry = crossings.get(entry_key)
        exit = crossings.get(exit_key)
        if entry is None or exit is None:
            raise ValueError("entry/exit observation references an unknown crossing")
        if not entry.is_entry or exit.is_entry:
            raise ValueError("entry/exit observation crossing types must be entry/exit")
        if (
            entry.vehicle_id != exit.vehicle_id
            or entry.subtrack_index != exit.subtrack_index
        ):
            raise ValueError("entry/exit observation crossings must share one subtrack")
        if entry.segment_start is None or exit.segment_start is None:
            raise ValueError("entry/exit observation requires stable segment starts")
        entry_progress = (
            entry.segment_start.refined_sequence_in_subtrack
            + entry.segment_fraction
        )
        exit_progress = (
            exit.segment_start.refined_sequence_in_subtrack
            + exit.segment_fraction
        )
        if entry_progress >= exit_progress:
            raise ValueError("entry crossing must precede exit crossing")
        rows.append((
            detect_id,
            intersection_id,
            observation.entry_bearing,
            observation.exit_bearing,
            crossing_ids[entry_key],
            crossing_ids[exit_key],
        ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "entry_exit_points",
            [
                "detect_id", "intersection_id", "entry_bearing", "exit_bearing",
                "entry_crossing_id", "exit_crossing_id",
            ],
            rows,
        )
    return len(rows)


def _refine_key(identity: RefinedPointRef) -> tuple[str, int, int]:
    return identity.point_kind, identity.point_id, identity.subtrack_index


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


def _copy_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    observe: bool = True,
) -> None:
    # A streaming caller owns cumulative progress across its short COPYs.
    # Standalone COPYs retain their own heartbeat and local row count.
    with operation_heartbeat(f"写入 {table}") if observe else nullcontext():
        if not rows:
            return
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
            for index, row in enumerate(rows, 1):
                copy.write_row(row)
                if observe and index % 10_000 == 0:
                    heartbeat_progress(index, total=len(rows))
            if observe:
                heartbeat_progress(len(rows), total=len(rows))


def _chunked_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    batch_size: int,
) -> None:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return
        size = min(batch_size, max(1, 65_535 // len(columns)))
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            placeholders = ", ".join(
                f"({', '.join(['%s'] * len(columns))})" for _ in batch
            )
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders}",
                [value for row in batch for value in row],
            )


def _chunked_insert_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    returning: list[str],
    *,
    batch_size: int,
) -> list[tuple[Any, ...]]:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return []
        size = min(batch_size, max(1, 65_535 // len(columns)))
        returned: list[tuple[Any, ...]] = []
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            placeholders = ", ".join(
                f"({', '.join(['%s'] * len(columns))})" for _ in batch
            )
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders} "
                f"RETURNING {', '.join(returning)}",
                [value for row in batch for value in row],
            )
            returned.extend(cur.fetchall())
        return returned


def _chunked_template(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    value_template: str,
    *,
    batch_size: int,
) -> None:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return
        params_per_row = len(rows[0])
        size = min(batch_size, max(1, 65_535 // params_per_row))
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                + ", ".join(f"({value_template})" for _ in batch),
                [value for row in batch for value in row],
            )


def _chunked_template_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    value_template: str,
    returning: list[str],
) -> list[tuple[Any, ...]]:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return []
        returned: list[tuple[Any, ...]] = []
        params_per_row = len(rows[0])
        size = max(1, 65_535 // params_per_row)
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                + ", ".join(f"({value_template})" for _ in batch)
                + f" RETURNING {', '.join(returning)}",
                [value for row in batch for value in row],
            )
            returned.extend(cur.fetchall())
        return returned


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError(f"{label} must be a JSON object")
