"""Bounded semantic comparison of two completed Pipeline lineages."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable, Iterator
from uuid import UUID

from ..db.schema import require_current_schema


@dataclass(frozen=True)
class TableSpec:
    name: str
    run_kind: str
    query: str
    columns: tuple[tuple[str, str], ...]


_SEMANTIC_KEYS = """
WITH member_evidence AS (
 SELECT t.detect_id,t.candidate_intersection_id,
        jsonb_build_array('turn',r.subtrack_index,r.sequence_in_subtrack)::text AS evidence
 FROM turn_events t JOIN resolved_subtrack_points r
   ON (r.id,r.detect_id)=(t.resolved_point_id,t.detect_id)
 WHERE t.candidate_intersection_id IS NOT NULL
 UNION ALL
 SELECT x.detect_id,x.candidate_intersection_id,
        CASE WHEN (a.subtrack_index,a.sequence_in_subtrack)
                       <= (b.subtrack_index,b.sequence_in_subtrack)
             THEN jsonb_build_array('cross',a.subtrack_index,a.sequence_in_subtrack,
                                    b.subtrack_index,b.sequence_in_subtrack)::text
             ELSE jsonb_build_array('cross',b.subtrack_index,b.sequence_in_subtrack,
                                    a.subtrack_index,a.sequence_in_subtrack)::text END
 FROM tracks_cross x
 JOIN resolved_subtrack_points a ON (a.id,a.detect_id)=(x.from_segment_start_point_id,x.detect_id)
 JOIN resolved_subtrack_points b ON (b.id,b.detect_id)=(x.to_segment_start_point_id,x.detect_id)
 WHERE x.candidate_intersection_id IS NOT NULL
), candidate_keys AS (
 SELECT c.id,c.detect_id,string_agg(m.evidence,',' ORDER BY m.evidence) AS business_key
 FROM candidate_intersections c LEFT JOIN member_evidence m
   ON (m.candidate_intersection_id,m.detect_id)=(c.id,c.detect_id)
 GROUP BY c.id,c.detect_id
), crossing_keys AS (
 SELECT c.id,c.detect_id,
        CASE WHEN k.business_key IS NULL THEN NULL ELSE
          jsonb_build_array(k.business_key,r.subtrack_index,r.sequence_in_subtrack,c.crossing_type)::text
        END AS business_key
 FROM candidate_entry_exit_points c
 JOIN candidate_keys k ON (k.id,k.detect_id)=(c.candidate_intersection_id,c.detect_id)
 JOIN resolved_subtrack_points r ON (r.id,r.detect_id)=(c.segment_start_point_id,c.detect_id)
), intersection_keys AS (
 SELECT i.id,i.detect_id,k.business_key
 FROM intersections i JOIN candidate_keys k
   ON (k.id,k.detect_id)=(i.candidate_intersection_id,i.detect_id)
) """


TABLES = (
    TableSpec("refine.kept_points", "refine", """
      SELECT source_row_key, vehicle_id, subtrack_index, sequence_in_subtrack
      FROM kept_trajectory_points WHERE refine_id=%s ORDER BY source_row_key
    """, (("vehicle_id", "exact"), ("subtrack_index", "exact"), ("sequence", "exact"))),
    TableSpec("refine.injected_points", "refine", """
      SELECT jsonb_build_array(vehicle_id,subtrack_index,after_sequence_in_subtrack,generated_reason)::text,
             timestamp,lon,lat,x,y,heading,speed
      FROM injected_points WHERE refine_id=%s
      ORDER BY vehicle_id,subtrack_index,after_sequence_in_subtrack,generated_reason
    """, (("timestamp", "float"), ("lon", "degree"), ("lat", "degree"),
           ("x", "metre"), ("y", "metre"), ("heading", "float"), ("speed", "float"))),
    TableSpec("refine.excluded_points", "refine", """
      SELECT source_row_key,reason FROM excluded_points WHERE refine_id=%s ORDER BY source_row_key
    """, (("reason", "exact"),)),
    TableSpec("detect.resolved_points", "detect", _SEMANTIC_KEYS + """
      SELECT jsonb_build_array(r.subtrack_index,r.sequence_in_subtrack)::text,
             r.refined_sequence_in_subtrack,
             CASE WHEN k.id IS NOT NULL THEN jsonb_build_array('kept',k.source_row_key)::text
                  WHEN n.id IS NOT NULL THEN jsonb_build_array('injected',n.vehicle_id,n.subtrack_index,
                                                               n.after_sequence_in_subtrack,n.generated_reason)::text
                  ELSE jsonb_build_array('crossing',COALESCE(q.business_key,'__UNCOMPARABLE_CROSSING__'))::text
             END
      FROM resolved_subtrack_points r
      LEFT JOIN kept_trajectory_points k ON k.id=r.kept_point_id
      LEFT JOIN injected_points n ON n.id=r.injected_point_id
      LEFT JOIN candidate_entry_exit_points c ON c.id=r.crossing_point_id AND c.detect_id=r.detect_id
      LEFT JOIN crossing_keys q ON (q.id,q.detect_id)=(c.id,c.detect_id)
      WHERE r.detect_id=%s ORDER BY r.subtrack_index,r.sequence_in_subtrack
    """, (("refined_sequence", "exact"), ("source_identity", "exact"))),
    TableSpec("detect.candidate_intersections", "detect", _SEMANTIC_KEYS + """
      SELECT COALESCE(k.business_key,'__UNCOMPARABLE_CANDIDATE__'),
             c.centroid_x,c.centroid_y,c.centroid_lon,c.centroid_lat,c.radius_m,
             ST_AsGeoJSON(c.geom),c.contributors::text,c.critical_contributors::text
      FROM candidate_intersections c JOIN candidate_keys k ON (k.id,k.detect_id)=(c.id,c.detect_id)
      WHERE c.detect_id=%s ORDER BY k.business_key NULLS FIRST
    """, (("centroid_x", "metre"), ("centroid_y", "metre"),
           ("centroid_lon", "degree"), ("centroid_lat", "degree"), ("radius_m", "metre"),
           ("geometry", "geojson"), ("contributors", "json_float"), ("critical", "json"))),
    TableSpec("detect.tracks_cross", "detect", _SEMANTIC_KEYS + """
      SELECT CASE WHEN (a.subtrack_index,a.sequence_in_subtrack)
                            <= (b.subtrack_index,b.sequence_in_subtrack)
                  THEN jsonb_build_array(a.subtrack_index,a.sequence_in_subtrack,
                                         b.subtrack_index,b.sequence_in_subtrack)::text
                  ELSE jsonb_build_array(b.subtrack_index,b.sequence_in_subtrack,
                                         a.subtrack_index,a.sequence_in_subtrack)::text END,
             CASE WHEN x.candidate_intersection_id IS NULL THEN '__NO_CANDIDATE__'
                  ELSE COALESCE(k.business_key,'__UNCOMPARABLE_CANDIDATE__') END,x.x,x.y,x.lon,x.lat
      FROM tracks_cross x
      JOIN resolved_subtrack_points a ON (a.id,a.detect_id)=(x.from_segment_start_point_id,x.detect_id)
      JOIN resolved_subtrack_points b ON (b.id,b.detect_id)=(x.to_segment_start_point_id,x.detect_id)
      LEFT JOIN candidate_keys k ON (k.id,k.detect_id)=(x.candidate_intersection_id,x.detect_id)
      WHERE x.detect_id=%s ORDER BY 1
    """, (("candidate", "exact"), ("x", "metre"), ("y", "metre"),
           ("lon", "degree"), ("lat", "degree"))),
    TableSpec("detect.turn_events", "detect", _SEMANTIC_KEYS + """
      SELECT jsonb_build_array(r.subtrack_index,r.sequence_in_subtrack)::text,
             CASE WHEN t.candidate_intersection_id IS NULL THEN '__NO_CANDIDATE__'
                  ELSE COALESCE(k.business_key,'__UNCOMPARABLE_CANDIDATE__') END,
             t.entry_bearing,t.exit_bearing,
             t.turn_deg,t.score,t.g_star_z
      FROM turn_events t JOIN resolved_subtrack_points r
        ON (r.id,r.detect_id)=(t.resolved_point_id,t.detect_id)
      LEFT JOIN candidate_keys k ON (k.id,k.detect_id)=(t.candidate_intersection_id,t.detect_id)
      WHERE t.detect_id=%s ORDER BY 1
    """, (("candidate", "exact"), ("entry_bearing", "float"), ("exit_bearing", "float"),
           ("turn_deg", "float"), ("score", "float"), ("g_star_z", "nullable_float"))),
    TableSpec("detect.candidate_entry_exit_points", "detect", _SEMANTIC_KEYS + """
      SELECT COALESCE(k.business_key,'__UNCOMPARABLE_CROSSING__'),
             c.crossing_x,c.crossing_y,c.crossing_lon,c.crossing_lat
      FROM candidate_entry_exit_points c JOIN crossing_keys k ON (k.id,k.detect_id)=(c.id,c.detect_id)
      WHERE c.detect_id=%s ORDER BY k.business_key NULLS FIRST
    """, (("crossing_x", "metre"), ("crossing_y", "metre"),
           ("crossing_lon", "degree"), ("crossing_lat", "degree"))),
    TableSpec("detect.intersections", "detect", _SEMANTIC_KEYS + """
      SELECT COALESCE(k.business_key,'__UNCOMPARABLE_INTERSECTION__'),
             i.branch_centers_deg::text, i.transition_counts::text,
             i.observation_count,i.clip_radius_m,ST_AsGeoJSON(i.geom)
      FROM intersections i JOIN intersection_keys k ON (k.id,k.detect_id)=(i.id,i.detect_id)
      WHERE i.detect_id=%s ORDER BY k.business_key NULLS FIRST
    """, (("branches", "json_float"), ("transitions", "json"), ("observations", "exact"),
           ("clip_radius_m", "metre"), ("geometry", "geojson"))),
    TableSpec("detect.entry_exit", "detect", _SEMANTIC_KEYS + """
      , crossing_keys2 AS (SELECT * FROM crossing_keys)
      SELECT jsonb_build_array(COALESCE(k.business_key,'__UNCOMPARABLE_INTERSECTION__'),
                               e.business_key,x.business_key)::text,
             p.entry_bearing,p.exit_bearing
      FROM entry_exit_points p
      JOIN intersection_keys k ON (k.id,k.detect_id)=(p.intersection_id,p.detect_id)
      JOIN crossing_keys e ON (e.id,e.detect_id)=(p.entry_crossing_id,p.detect_id)
      JOIN crossing_keys2 x ON (x.id,x.detect_id)=(p.exit_crossing_id,p.detect_id)
      WHERE p.detect_id=%s ORDER BY 1
    """, (("entry_bearing", "float"), ("exit_bearing", "float"))),
    TableSpec("fitting.centerlines", "fit", _SEMANTIC_KEYS + """
      SELECT jsonb_build_array(c.variant,COALESCE(a.business_key,'__UNCOMPARABLE_INTERSECTION__'),
                               COALESCE(b.business_key,'__UNCOMPARABLE_INTERSECTION__'))::text,c.length_m,
             c.forward_clip_count,c.reverse_clip_count,ST_AsGeoJSON(c.geom)
      FROM centerlines c
      JOIN intersection_keys a ON (a.id,a.detect_id)=(c.from_intersection_id,c.detect_id)
      JOIN intersection_keys b ON (b.id,b.detect_id)=(c.to_intersection_id,c.detect_id)
      WHERE c.fit_id=%s ORDER BY 1
    """, (("length_m", "metre"), ("forward_clips", "exact"), ("reverse_clips", "exact"),
           ("geometry", "geojson"))),
    TableSpec("fitting.source_clips", "fit", _SEMANTIC_KEYS + """
      , all_crossings AS (SELECT * FROM crossing_keys), line_keys AS (
        SELECT c.id,c.detect_id,
               jsonb_build_array(c.variant,COALESCE(a.business_key,'__UNCOMPARABLE_INTERSECTION__'),
                                 COALESCE(b.business_key,'__UNCOMPARABLE_INTERSECTION__'))::text AS business_key
        FROM centerlines c
        JOIN intersection_keys a ON (a.id,a.detect_id)=(c.from_intersection_id,c.detect_id)
        JOIN intersection_keys b ON (b.id,b.detect_id)=(c.to_intersection_id,c.detect_id)
        WHERE c.fit_id=%s)
      SELECT jsonb_build_array(l.business_key,s.input_ordinal)::text,
             e.business_key,x.business_key,s.reverse_to_centerline_direction
      FROM centerline_source_clips s
      JOIN line_keys l ON (l.id,l.detect_id)=(s.centerline_id,s.detect_id)
      JOIN crossing_keys e ON (e.id,e.detect_id)=(s.start_crossing_id,s.detect_id)
      JOIN all_crossings x ON (x.id,x.detect_id)=(s.end_crossing_id,s.detect_id)
      ORDER BY 1
    """, (("start_crossing", "exact"), ("end_crossing", "exact"), ("reverse", "exact"))),
    TableSpec("network.topology", "fit", _SEMANTIC_KEYS + """
      SELECT jsonb_build_array('edge',COALESCE(a.business_key,'__UNCOMPARABLE_INTERSECTION__'),
                               COALESCE(b.business_key,'__UNCOMPARABLE_INTERSECTION__'))::text,c.variant
      FROM centerlines c
      JOIN intersection_keys a ON (a.id,a.detect_id)=(c.from_intersection_id,c.detect_id)
      JOIN intersection_keys b ON (b.id,b.detect_id)=(c.to_intersection_id,c.detect_id)
      WHERE c.fit_id=%s AND c.variant='combined' ORDER BY 1
    """, (("variant", "exact"),)),
)

_TOLERANCE = {"metre": 1e-6, "degree": 1e-9, "float": 1e-12}
_EXCLUSIONS = {
    "network.topology": [
        "surrogate primary keys", "run UUIDs", "timestamps",
        "Network issues/report and include_isolated_nodes (not persisted)",
    ],
}


def canonical_endpoint_pair(
    left: tuple[int, int], right: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the stable identity of an undirected resolved-segment pair."""
    return (left, right) if left <= right else (right, left)


def _json_floats(value: Any) -> Iterator[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield float(value)
    elif isinstance(value, list):
        for item in value:
            yield from _json_floats(item)
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _json_floats(value[key])


def _compare_value(left: Any, right: Any, kind: str) -> tuple[bool, float]:
    if kind == "nullable_float":
        if left is None or right is None:
            return left is None and right is None, 0.0 if left is right else math.inf
        kind = "float"
    if kind in _TOLERANCE:
        values = (float(left), float(right))
        if not all(math.isfinite(value) for value in values):
            return False, math.inf
        error = abs(values[0] - values[1])
        return error <= _TOLERANCE[kind], error
    if kind in {"json", "json_float", "geojson"}:
        left_json = json.loads(left) if isinstance(left, str) else left
        right_json = json.loads(right) if isinstance(right, str) else right
        if kind == "json":
            return left_json == right_json, 0.0
        left_shape = json.loads(json.dumps(left_json))
        right_shape = json.loads(json.dumps(right_json))
        left_values, right_values = list(_json_floats(left_shape)), list(_json_floats(right_shape))
        def erase(value: Any) -> Any:
            if isinstance(value, (int, float)) and not isinstance(value, bool): return 0.0
            if isinstance(value, list): return [erase(item) for item in value]
            if isinstance(value, dict): return {key: erase(item) for key, item in value.items()}
            return value
        if erase(left_shape) != erase(right_shape) or len(left_values) != len(right_values):
            return False, math.inf
        if not all(math.isfinite(value) for value in (*left_values, *right_values)):
            return False, math.inf
        error = max((abs(a - b) for a, b in zip(left_values, right_values, strict=True)), default=0.0)
        tolerance = _TOLERANCE["degree"] if kind == "geojson" else _TOLERANCE["float"]
        return error <= tolerance, error
    return left == right, 0.0


def compare_rows(
    name: str, columns: tuple[tuple[str, str], ...], left: Iterable[tuple[Any, ...]],
    right: Iterable[tuple[Any, ...]],
) -> dict[str, Any]:
    left_iter, right_iter = iter(left), iter(right)
    count = 0
    maximum: dict[str, float | None] = {
        column: 0.0 for column, kind in columns if kind != "exact"
    }
    left_seen = right_seen = 0
    previous: str | None = None
    failure: str | None = None
    while True:
        a, b = next(left_iter, None), next(right_iter, None)
        left_seen += a is not None
        right_seen += b is not None
        if a is None or b is None:
            if a != b: failure = "row_count_mismatch"
            break
        key_a, key_b = str(a[0]), str(b[0])
        if "__UNCOMPARABLE_" in key_a or "__UNCOMPARABLE_" in key_b:
            failure = "unreliable_business_identity"
            break
        if key_a == previous or key_b == previous:
            failure = "non_unique_business_identity"
            break
        if key_a != key_b:
            failure = f"business_identity_mismatch:{key_a!r}:{key_b!r}"
            break
        previous = key_a
        for index, (column, kind) in enumerate(columns, 1):
            equal, error = _compare_value(a[index], b[index], kind)
            if kind != "exact":
                maximum[column] = (
                    max(maximum[column] or 0.0, error) if math.isfinite(error) else None
                )
            if not equal:
                failure = f"value_mismatch:{key_a}:{column}"
                break
        count += 1
        if failure: break
    return {"table": name, "baseline_rows_seen": left_seen,
            "candidate_rows_seen": right_seen, "rows_compared": count, "maximum_error": maximum,
            "not_comparable": _EXCLUSIONS.get(
                name, ["surrogate primary keys", "run UUIDs", "timestamps"]),
            "passed": failure is None, "failure": failure}


def _rows(conn: Any, spec: TableSpec, run_id: UUID, side: str, batch_size: int) -> Iterator[tuple[Any, ...]]:
    with conn.cursor(name=f"semantic_diff_{side}_{spec.name.replace('.', '_')}") as cursor:
        cursor.itersize = batch_size
        cursor.execute(spec.query, (run_id,))
        yield from cursor


def _lineage(conn: Any, runs: dict[str, UUID]) -> tuple[Any, ...] | None:
    return conn.execute("""
      SELECT r.status,d.status,f.status,r.source_table,r.source_fingerprint,
             r.source_srid,r.processing_srid,r.projection_config,
             r.parameters-'execution_config',d.parameters-'execution_config',
             f.clip_algorithm,f.clip_version,f.fitting_algorithm,f.fitting_version,
             f.parameters-'execution_config'
      FROM refine_runs r
      JOIN detect_runs d ON d.id=%s AND d.refine_id=r.id
      JOIN fit_runs f ON f.id=%s AND f.detect_id=d.id
      WHERE r.id=%s
    """, (runs["detect"], runs["fit"], runs["refine"])).fetchone()


def compare_runs(conn: Any, baseline: dict[str, UUID], candidate: dict[str, UUID], *, batch_size: int = 2000) -> dict[str, Any]:
    conn.execute("SET TRANSACTION READ ONLY")
    require_current_schema(conn)
    metadata = {label: _lineage(conn, runs) for label, runs in
                (("baseline", baseline), ("candidate", candidate))}
    for label, row in metadata.items():
        if row is None or row[:3] != ("completed", "completed", "completed"):
            return {"version": 1, "passed": False,
                    "error": f"preflight:lineage:{label}", "tables": []}
    left, right = metadata["baseline"], metadata["candidate"]
    assert left is not None and right is not None
    checks = (
        ("source_table", slice(3, 4)), ("source_fingerprint", slice(4, 5)),
        ("projection_config", slice(5, 8)), ("refine_semantic_config", slice(8, 9)),
        ("detect_semantic_config", slice(9, 10)), ("fit_semantic_config", slice(10, 15)),
    )
    for category, fields in checks:
        if left[fields] != right[fields]:
            return {"version": 1, "passed": False,
                    "error": f"preflight:{category}", "tables": []}
    tables = [compare_rows(spec.name, spec.columns,
                           _rows(conn, spec, baseline[spec.run_kind], "baseline", batch_size),
                           _rows(conn, spec, candidate[spec.run_kind], "candidate", batch_size))
              for spec in TABLES]
    return {"version": 1, "passed": all(table["passed"] for table in tables), "tables": tables}
