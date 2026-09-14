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
from dataclasses import dataclass
from typing import Any

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
) -> str:
    fit_id = str(uuid.uuid4())
    parameters = {
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
    batch_size: int = _BATCH,
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
        (refine_id, item.point_ref.source_row_key, item.reason)
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
                point.point_ref.source_row_key,
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


def persist_detect_result(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    *,
    batch_size: int = _BATCH,
) -> DetectWriteCounts:
    """Persist the complete Detect graph and resolved p/i/c sequence."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refine_id, status FROM detect_runs WHERE id = %s FOR UPDATE",
            (detect_id,),
        )
        run = cur.fetchone()
    if run is None or str(run[1]) != "running":
        raise ValueError(f"Detect Run {detect_id} is not running")
    refine_id = str(run[0])

    refine_members = _load_refine_members(conn, refine_id)
    _validate_refine_member_vehicles(refine_members)
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

    resolved_sequence_started = time.perf_counter()
    resolved_source_count = len(refine_members) + len(complete_crossings)
    with conn.cursor() as cur:
        resolved_ids = _reserve_ids(cur, "resolved_subtrack_points", resolved_source_count)
    refined_resolved_ids = resolved_ids[:len(refine_members)]
    resolved_by_refine_key = {
        _refine_key(member.identity): resolved_id
        for member, resolved_id in zip(
            refine_members,
            refined_resolved_ids,
            strict=True,
        )
    }
    complete_crossing_resolved_ids = resolved_ids[len(refine_members):]

    resolved_rows = _build_resolved_rows(
        detect_id,
        refine_members,
        refined_resolved_ids,
        complete_crossings,
        complete_crossing_resolved_ids,
        crossing_id_by_calculation,
    )
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "resolved_subtrack_points",
            [
                "id",
                "detect_id",
                "subtrack_index",
                "refined_sequence_in_subtrack",
                "sequence_in_subtrack",
                "kept_point_id",
                "injected_point_id",
                "crossing_point_id",
            ],
            resolved_rows,
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
    turn_membership, cross_membership = _candidate_memberships(
        ordered_clusters,
        candidate_by_calculation,
    )
    tracks_cross_count = _insert_tracks_cross(
        conn,
        detect_id,
        result,
        resolved_by_refine_key,
        cross_membership,
    )
    turn_event_count = _insert_turn_events(
        conn,
        detect_id,
        result,
        resolved_by_refine_key,
        turn_membership,
    )
    _insert_candidate_crossings(
        conn,
        detect_id,
        pending_crossings,
        crossing_ids,
        candidate_by_calculation,
        resolved_by_refine_key,
    )
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
        resolved_point_count=len(resolved_rows),
        tracks_cross_count=tracks_cross_count,
        turn_event_count=turn_event_count,
        candidate_intersection_count=len(ordered_clusters),
        candidate_entry_exit_point_count=len(pending_crossings),
        intersection_count=len(complete_topologies),
        entry_exit_point_count=entry_exit_count,
        resolved_sequence_elapsed_s=resolved_sequence_elapsed,
    )


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
    clips_by_id = {clip.clip_id: clip for clip in clips.clips}
    manifest_rows: list[tuple[Any, ...]] = []
    for centerline_id, centerline in zip(ordered_ids, centerlines, strict=True):
        for ordinal, clip_id in enumerate(centerline.source_clip_ids):
            clip = clips_by_id[clip_id]
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


def _load_refine_members(conn: Any, refine_id: str) -> list[_RefineMember]:
    with conn.cursor() as cur:
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
        raw_rows = list(cur.fetchall())
    members: list[_RefineMember] = []
    current_subtrack: int | None = None
    sequence = 0
    for kind, point_id, vehicle, subtrack, _anchor in raw_rows:
        subtrack = int(subtrack)
        if subtrack != current_subtrack:
            current_subtrack = subtrack
            sequence = 0
        members.append(_RefineMember(
            identity=RefinedPointRef(
                point_kind=str(kind),  # type: ignore[arg-type]
                point_id=int(point_id),
                subtrack_index=subtrack,
                refined_sequence_in_subtrack=sequence,
            ),
            vehicle_id=str(vehicle),
        ))
        sequence += 1
    return members


def _validate_refine_member_vehicles(members: list[_RefineMember]) -> None:
    vehicles_by_subtrack: dict[int, set[str]] = {}
    for member in members:
        vehicles_by_subtrack.setdefault(
            member.identity.subtrack_index,
            set(),
        ).add(member.vehicle_id)
    invalid = {
        subtrack: vehicles
        for subtrack, vehicles in vehicles_by_subtrack.items()
        if len(vehicles) != 1
    }
    if invalid:
        raise ValueError(f"a subtrack contains multiple vehicles: {invalid}")


def _build_resolved_rows(
    detect_id: str,
    refine_members: list[_RefineMember],
    refined_ids: tuple[int, ...],
    complete_crossings: list[tuple[HotspotCluster, Any]],
    crossing_resolved_ids: tuple[int, ...],
    crossing_id_by_calculation: dict[tuple[int, int], int],
) -> list[tuple[Any, ...]]:
    by_subtrack: dict[int, list[tuple[tuple[Any, ...], tuple[Any, ...]]]] = {}
    for member, resolved_id in zip(refine_members, refined_ids, strict=True):
        identity = member.identity
        kept_id = identity.point_id if identity.point_kind == "kept" else None
        injected_id = identity.point_id if identity.point_kind == "injected" else None
        by_subtrack.setdefault(identity.subtrack_index, []).append((
            (
                float(identity.refined_sequence_in_subtrack),
                1,
                -1,
                -1,
                resolved_id,
            ),
            (
                resolved_id,
                detect_id,
                identity.subtrack_index,
                identity.refined_sequence_in_subtrack,
                kept_id,
                injected_id,
                None,
            ),
        ))
    for (cluster, crossing), resolved_id in zip(
        complete_crossings,
        crossing_resolved_ids,
        strict=True,
    ):
        if crossing.segment_start is None:
            raise ValueError("a persisted crossing requires a stable segment start")
        identity = crossing.segment_start
        key = (cluster.cluster_calculate_index, crossing.crossing_calculate_index)
        crossing_id = crossing_id_by_calculation[key]
        # A root at t=1 belongs immediately before the segment endpoint; a
        # t=0 root belongs immediately after its segment start. Interior roots
        # cannot tie a refined point. Within one event batch, exits precede
        # entries as required by the active-set sequence contract.
        endpoint_placement = 0 if crossing.segment_fraction >= 1.0 - 1e-12 else 2
        by_subtrack.setdefault(identity.subtrack_index, []).append((
            (
                identity.refined_sequence_in_subtrack + crossing.segment_fraction,
                endpoint_placement,
                0 if not crossing.is_entry else 1,
                cluster.cluster_calculate_index,
                crossing_id,
            ),
            (
                resolved_id,
                detect_id,
                identity.subtrack_index,
                None,
                None,
                None,
                crossing_id,
            ),
        ))
    rows: list[tuple[Any, ...]] = []
    for subtrack_index in sorted(by_subtrack):
        ordered = sorted(by_subtrack[subtrack_index], key=lambda item: item[0])
        for sequence, (_sort_key, row) in enumerate(ordered):
            rows.append((*row[:4], sequence, *row[4:]))
    return rows


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


def _candidate_memberships(
    clusters: list[HotspotCluster],
    candidate_ids: dict[int, int],
) -> tuple[dict[int, int], dict[int, int]]:
    turns: dict[int, int] = {}
    crosses: dict[int, int] = {}
    for cluster in clusters:
        candidate_id = candidate_ids[cluster.cluster_calculate_index]
        for event in cluster.events:
            if event.source == "turn":
                _assign_unique(turns, event.event_calculate_index, candidate_id)
            elif (
                event.source == "orthogonal_cross_track"
                and event.cross_track_calculate_index is not None
            ):
                _assign_unique(
                    crosses,
                    event.cross_track_calculate_index,
                    candidate_id,
                )
    return turns, crosses


def _insert_tracks_cross(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    resolved_by_refine_key: dict[tuple[str, int, int], int],
    membership: dict[int, int],
) -> int:
    rows: list[tuple[Any, ...]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for candidate in result.cross_track_candidates:
        if candidate.from_segment_start is None or candidate.to_segment_start is None:
            raise ValueError("a persisted track cross requires stable segment starts")
        from_id = resolved_by_refine_key[_refine_key(candidate.from_segment_start)]
        to_id = resolved_by_refine_key[_refine_key(candidate.to_segment_start)]
        from_id, to_id = sorted((from_id, to_id))
        if from_id == to_id or (from_id, to_id) in seen_pairs:
            raise ValueError("duplicate/degenerate track cross segment pair")
        seen_pairs.add((from_id, to_id))
        rows.append((
            detect_id,
            membership.get(candidate.cross_track_calculate_index),
            from_id,
            to_id,
            candidate.x,
            candidate.y,
            candidate.lon,
            candidate.lat,
        ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "tracks_cross",
            [
                "detect_id", "candidate_intersection_id",
                "from_segment_start_point_id", "to_segment_start_point_id",
                "x", "y", "lon", "lat",
            ],
            rows,
        )
    return len(rows)


def _insert_turn_events(
    conn: Any,
    detect_id: str,
    result: IntersectResult,
    resolved_by_refine_key: dict[tuple[str, int, int], int],
    membership: dict[int, int],
) -> int:
    hotspot_z = {
        event.event_calculate_index: event.g_star_z
        for event in result.hotspot_events
    }
    rows: list[tuple[Any, ...]] = []
    for event in result.turn_events:
        if event.refined_point_ref is None:
            raise ValueError("a persisted turn event requires a stable resolved point")
        rows.append((
            detect_id,
            membership.get(event.event_calculate_index),
            resolved_by_refine_key[_refine_key(event.refined_point_ref)],
            event.entry_bearing,
            event.exit_bearing,
            event.turn_deg,
            event.score,
            hotspot_z.get(event.event_calculate_index),
        ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "turn_events",
            [
                "detect_id", "candidate_intersection_id", "resolved_point_id",
                "entry_bearing", "exit_bearing", "turn_deg", "score", "g_star_z",
            ],
            rows,
        )
    return len(rows)


def _insert_candidate_crossings(
    conn: Any,
    detect_id: str,
    pending: list[tuple[HotspotCluster, Any]],
    crossing_ids: tuple[int, ...],
    candidate_ids: dict[int, int],
    resolved_by_refine_key: dict[tuple[str, int, int], int],
) -> None:
    rows: list[tuple[Any, ...]] = []
    for (cluster, crossing), crossing_id in zip(pending, crossing_ids, strict=True):
        if crossing.segment_start is None:
            raise ValueError("a persisted crossing requires a stable segment start")
        rows.append((
            crossing_id,
            detect_id,
            candidate_ids[cluster.cluster_calculate_index],
            resolved_by_refine_key[_refine_key(crossing.segment_start)],
            1 if crossing.is_entry else 0,
            crossing.crossing_x,
            crossing.crossing_y,
            crossing.crossing_lon,
            crossing.crossing_lat,
        ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "candidate_entry_exit_points",
            [
                "id", "detect_id", "candidate_intersection_id",
                "segment_start_point_id", "crossing_type", "crossing_x",
                "crossing_y", "crossing_lon", "crossing_lat",
            ],
            rows,
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
) -> None:
    if not rows:
        return
    with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def _chunked_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    batch_size: int,
) -> None:
    if not rows:
        return
    size = min(batch_size, max(1, 65_535 // len(columns)))
    for offset in range(0, len(rows), size):
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
    if not rows:
        return []
    size = min(batch_size, max(1, 65_535 // len(columns)))
    returned: list[tuple[Any, ...]] = []
    for offset in range(0, len(rows), size):
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
    if not rows:
        return
    params_per_row = len(rows[0])
    size = min(batch_size, max(1, 65_535 // params_per_row))
    for offset in range(0, len(rows), size):
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
    if not rows:
        return []
    returned: list[tuple[Any, ...]] = []
    params_per_row = len(rows[0])
    size = max(1, 65_535 // params_per_row)
    for offset in range(0, len(rows), size):
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
