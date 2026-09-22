from __future__ import annotations

import math
import os
import re

import pytest

from uuid import uuid4

from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.validation.semantic_diff import (
    TABLES, canonical_endpoint_pair, compare_rows, compare_runs,
)


class _Cursor:
    def __init__(self, name: str, rows: list[tuple]) -> None:
        self.name = name
        self.rows = rows
        self.itersize = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple) -> None:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH"))
        assert len(params) == sql.count("%s")

    def __iter__(self):
        return iter(self.rows)


class _Result:
    def __init__(self, row: tuple) -> None:
        self.row = row

    def fetchone(self):
        return self.row


_METADATA = (
    "completed", "completed", "completed", "public.source", {"sha256": "same"},
    4326, 32650, {"coordinate_semantics_version": 2}, {"refine": 1}, {"detect": 1},
    "clip", "1", "fit", "1", {"fitting": 1},
)


class _Connection:
    def __init__(self, candidate_metadata: tuple = _METADATA) -> None:
        self.statements: list[str] = []
        self.metadata = iter([_METADATA, candidate_metadata])
        self.rows_opened = 0

    def execute(self, sql: str, _params: tuple = ()):
        self.statements.append(sql)
        assert sql.strip().upper().startswith(("SET TRANSACTION READ ONLY", "SELECT"))
        return _Result(()) if sql.strip().upper().startswith("SET") else _Result(next(self.metadata))

    def cursor(self, name: str | None = None):
        if name is None:
            return _SchemaCursor()
        self.rows_opened += 1
        spec = next(spec for spec in TABLES if spec.name.replace(".", "_") in name)
        row = ("stable-key", *(0.0 if kind != "exact" else "same" for _, kind in spec.columns))
        return _Cursor(name, [row])


class _SchemaCursor:
    def __init__(self) -> None:
        self.responses = iter([("pipeline_schema_versions",), (7,)])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, _params: tuple = ()) -> None:
        assert sql.lstrip().upper().startswith("SELECT")

    def fetchone(self):
        return next(self.responses)


def test_compare_runs_is_read_only_and_uses_named_bounded_cursors() -> None:
    conn = _Connection()
    runs = {kind: uuid4() for kind in ("refine", "detect", "fit")}
    result = compare_runs(conn, runs, runs, batch_size=17)
    assert result["passed"] is True
    assert conn.statements[0] == "SET TRANSACTION READ ONLY"
    assert len(result["tables"]) == len(TABLES)


def test_semantic_queries_are_detect_scoped_and_fit_uses_detect_lineage() -> None:
    semantic = [spec for spec in TABLES if spec.name.startswith(("detect.", "fitting.", "network."))]
    assert all("target_detect AS (SELECT %s::uuid AS detect_id)" in spec.query
               for spec in semantic)
    assert all(spec.run_kinds[0] == "detect" for spec in semantic)
    assert next(spec for spec in TABLES if spec.name == "fitting.centerlines").run_kinds == (
        "detect", "fit",
    )
    assert "string_agg" not in semantic[0].query
    assert "min(m.evidence)" in semantic[0].query


def test_candidate_membership_detects_non_representative_change_and_bad_evidence() -> None:
    columns = (("representative", "exact"),)
    baseline = [('["turn",1,1]', '["turn",1,1]'),
                ('["turn",1,2]', '["turn",1,1]')]
    changed = [('["turn",1,1]', '["turn",1,1]'),
               ('["turn",1,3]', '["turn",1,1]')]
    assert not compare_rows("detect.candidate_membership", columns, baseline, changed)["passed"]
    duplicate = [baseline[0], baseline[0]]
    assert compare_rows("detect.candidate_membership", columns, duplicate, duplicate)[
        "failure"
    ] == "non_unique_business_identity"
    missing = [('__UNCOMPARABLE_MEMBER__', None)]
    assert compare_rows("detect.candidate_membership", columns, missing, missing)[
        "failure"
    ] == "unreliable_business_identity"


def test_geojson_queries_request_fifteen_decimal_digits() -> None:
    geometry_queries = [spec.query for spec in TABLES if ("geometry", "geojson") in spec.columns]
    assert geometry_queries and all("ST_AsGeoJSON(" in query and ",15)" in query
                                    for query in geometry_queries)


def test_surrogate_id_offsets_are_absent_from_normalized_rows() -> None:
    columns = (("source_identity", "exact"), ("lon", "degree"))
    baseline = [("track:1:0", "kept:source-7", 120.0)]
    candidate = [("track:1:0", "kept:source-7", 120.0)]
    result = compare_rows("detect.resolved_points", columns, baseline, candidate)
    assert result["passed"] is True
    assert result["not_comparable"] == ["surrogate primary keys", "run UUIDs", "timestamps"]


def test_candidate_id_offsets_pass_but_diagnostic_member_change_fails() -> None:
    columns = (("candidate", "exact"), ("x", "metre"))
    normalized = [("[1, 2, 3, 4]", "turn:[1,2]", 10.0)]
    assert compare_rows("detect.tracks_cross", columns, normalized, normalized)["passed"]
    changed = [("[1, 2, 3, 4]", "turn:[1,3]", 10.0)]
    assert not compare_rows("detect.tracks_cross", columns, normalized, changed)["passed"]


def test_tracks_cross_endpoint_reversal_has_same_undirected_identity() -> None:
    forward = canonical_endpoint_pair((3, 7), (1, 9))
    reverse = canonical_endpoint_pair((1, 9), (3, 7))
    assert forward == reverse == ((1, 9), (3, 7))
    tracks_query = next(spec.query for spec in TABLES if spec.name == "detect.tracks_cross")
    assert "CASE WHEN (a.subtrack_index,a.sequence_in_subtrack)" in tracks_query


def test_excluded_membership_change_fails() -> None:
    baseline = [("source-1", "stationary")]
    candidate = [("source-2", "stationary")]
    assert not compare_rows(
        "refine.excluded_points", (("reason", "exact"),), baseline, candidate,
    )["passed"]


@pytest.mark.parametrize(
    "index,category",
    [(3, "source_table"), (4, "source_fingerprint"), (7, "projection_config"),
     (8, "refine_semantic_config"), (9, "detect_semantic_config"),
     (14, "fit_semantic_config")],
)
def test_incompatible_lineage_fails_before_reading_tables(index: int, category: str) -> None:
    changed = list(_METADATA)
    changed[index] = {"different": True} if index != 3 else "other.source"
    conn = _Connection(tuple(changed))
    runs = {kind: uuid4() for kind in ("refine", "detect", "fit")}
    result = compare_runs(conn, runs, runs)
    assert result["error"] == f"preflight:{category}"
    assert conn.rows_opened == 0


def test_topology_change_fails_even_when_row_counts_match() -> None:
    result = compare_rows(
        "network.topology", (("variant", "exact"),),
        [("combined:A:B", "combined")], [("combined:A:C", "combined")],
    )
    assert result["passed"] is False
    assert result["failure"].startswith("business_identity_mismatch")


@pytest.mark.parametrize(
    "kind,tolerance", [("metre", 1e-6), ("degree", 1e-9), ("float", 1e-12)],
)
def test_numeric_tolerance_boundary_and_excess(kind: str, tolerance: float) -> None:
    columns = (("value", kind),)
    assert compare_rows("probe", columns, [("key", 0.0)], [("key", tolerance)])["passed"]
    assert not compare_rows("probe", columns, [("key", 0.0)], [("key", tolerance * 1.01)])["passed"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_fail_closed(value: float) -> None:
    result = compare_rows("probe", (("value", "float"),), [("key", value)], [("key", value)])
    assert result["passed"] is False
    assert result["maximum_error"]["value"] is None


def test_missing_intersection_business_evidence_fails_closed() -> None:
    result = compare_rows(
        "detect.intersections", (("observations", "exact"),),
        [("__UNCOMPARABLE_INTERSECTION__", 1)], [("__UNCOMPARABLE_INTERSECTION__", 1)],
    )
    assert result["failure"] == "unreliable_business_identity"


def test_duplicate_business_identity_fails_closed() -> None:
    rows = [("same", 1), ("same", 1)]
    assert compare_rows("probe", (("value", "exact"),), rows, rows)["failure"] == "non_unique_business_identity"


@pytest.fixture
def semantic_database():
    if not os.environ.get("ROADNET_TEST_DSN"):
        pytest.skip("ROADNET_TEST_DSN is not set")
    import psycopg
    from psycopg import sql

    dsn, schema = os.environ["ROADNET_TEST_DSN"], "semantic_diff_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = psycopg.conninfo.make_conninfo(dsn, options=f"-csearch_path={schema},public")
    try:
        with psycopg.connect(scoped) as conn:
            for _name, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                conn.execute(re.sub(
                    r"\b(geometry|ST_[a-z_]+)\s*\(", r"public.\1(", ddl, flags=re.I,
                ))
            yield conn
            conn.rollback()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_real_postgres_scopes_members_and_preserves_geometry_precision(semantic_database) -> None:
    conn = semantic_database
    baseline, candidate, unrelated = uuid4(), uuid4(), uuid4()
    probe_runs = {"refine": uuid4(), "detect": uuid4(), "fit": uuid4()}
    for spec in TABLES:
        conn.execute("EXPLAIN " + spec.query,
                     tuple(probe_runs[kind] for kind in spec.run_kinds)).fetchall()
    conn.execute("""
      CREATE TEMP TABLE resolved_subtrack_points(
        id bigint,detect_id uuid,subtrack_index bigint,sequence_in_subtrack bigint);
      CREATE TEMP TABLE turn_events(
        detect_id uuid,resolved_point_id bigint,candidate_intersection_id bigint);
      CREATE TEMP TABLE tracks_cross(
        detect_id uuid,candidate_intersection_id bigint,
        from_segment_start_point_id bigint,to_segment_start_point_id bigint);
      CREATE TEMP TABLE candidate_intersections(
        id bigint,detect_id uuid,centroid_x double precision,centroid_y double precision,
        centroid_lon double precision,centroid_lat double precision,radius_m double precision,
        geom public.geometry,contributors jsonb DEFAULT '{}',critical_contributors jsonb DEFAULT '[]');
      CREATE TEMP TABLE candidate_entry_exit_points(
        id bigint,detect_id uuid,candidate_intersection_id bigint,
        segment_start_point_id bigint,crossing_type integer);
      CREATE TEMP TABLE intersections(id bigint,detect_id uuid,candidate_intersection_id bigint)
    """)
    for detect_id, candidate_id, point_base in (
        (baseline, 10, 100), (candidate, 900, 500), (unrelated, 77, 700),
    ):
        conn.execute(
            "INSERT INTO candidate_intersections "
            "(id,detect_id,centroid_x,centroid_y,centroid_lon,centroid_lat,radius_m,geom) "
            "VALUES (%s,%s,0,0,120,30,1,public.ST_SetSRID(public.ST_MakePoint(%s,0),4326))",
            (candidate_id, detect_id,
             32.0 + (0.91e-9 if detect_id == candidate else -0.49e-9)),
        )
        conn.execute(
            "INSERT INTO resolved_subtrack_points"
            "(id,detect_id,subtrack_index,sequence_in_subtrack) VALUES "
            "(%s,%s,1,1),(%s,%s,1,2)",
            (point_base, detect_id, point_base + 1, detect_id),
        )
        conn.execute(
            "INSERT INTO turn_events VALUES (%s,%s,%s),(%s,%s,%s)",
            (detect_id, point_base, candidate_id, detect_id, point_base + 1, candidate_id),
        )
    membership = next(spec for spec in TABLES if spec.name == "detect.candidate_membership")
    rows = {
        detect_id: conn.execute(membership.query, (detect_id, detect_id)).fetchall()
        for detect_id in (baseline, candidate, unrelated)
    }
    assert len(rows[baseline]) == len(rows[candidate]) == len(rows[unrelated]) == 2
    assert compare_rows(membership.name, membership.columns,
                        rows[baseline], rows[candidate])["passed"]
    conn.execute(
        "UPDATE resolved_subtrack_points SET sequence_in_subtrack=NULL "
        "WHERE detect_id=%s AND sequence_in_subtrack=2", (candidate,),
    )
    null_evidence = conn.execute(membership.query, (candidate, candidate)).fetchall()
    assert any("__UNCOMPARABLE_MEMBER__" in row[0] for row in null_evidence)
    assert not compare_rows(membership.name, membership.columns,
                            null_evidence, null_evidence)["passed"]
    conn.execute(
        "UPDATE resolved_subtrack_points SET sequence_in_subtrack=3 "
        "WHERE detect_id=%s AND sequence_in_subtrack IS NULL", (candidate,),
    )
    changed = conn.execute(membership.query, (candidate, candidate)).fetchall()
    assert not compare_rows(membership.name, membership.columns, rows[baseline], changed)["passed"]
    candidate_spec = next(spec for spec in TABLES if spec.name == "detect.candidate_intersections")
    old_geometries = [conn.execute(
        "SELECT public.ST_AsGeoJSON(geom,9) FROM candidate_intersections WHERE detect_id=%s",
        (detect_id,),
    ).fetchone()[0] for detect_id in (baseline, candidate)]
    new_geometries = [conn.execute(
        "SELECT public.ST_AsGeoJSON(geom,15) FROM candidate_intersections WHERE detect_id=%s",
        (detect_id,),
    ).fetchone()[0] for detect_id in (baseline, candidate)]
    assert compare_rows("old_geometry", (("geometry", "geojson"),),
                        [("key", old_geometries[0])],
                        [("key", old_geometries[1])])["passed"]
    assert not compare_rows("new_geometry", (("geometry", "geojson"),),
                            [("key", new_geometries[0])],
                            [("key", new_geometries[1])])["passed"]
    left = conn.execute(candidate_spec.query, (baseline, baseline)).fetchall()
    right = conn.execute(candidate_spec.query, (candidate, candidate)).fetchall()
    assert not compare_rows(candidate_spec.name, candidate_spec.columns, left, right)["passed"]
    conn.execute(
        "INSERT INTO candidate_intersections "
        "(id,detect_id,centroid_x,centroid_y,centroid_lon,centroid_lat,radius_m,geom) "
        "VALUES (11,%s,0,0,120,30,1,public.ST_MakeEnvelope(0,0,1,1,4326))",
        (baseline,),
    )
    missing = conn.execute(membership.query, (baseline, baseline)).fetchall()
    assert any("__UNCOMPARABLE_MEMBER__" in row[0] for row in missing)
    assert not compare_rows(membership.name, membership.columns, missing, missing)["passed"]
    conn.execute("DELETE FROM candidate_intersections WHERE id=11 AND detect_id=%s", (baseline,))
    conn.execute(
        "INSERT INTO turn_events VALUES (%s,100,10)", (baseline,),
    )
    duplicate = conn.execute(membership.query, (baseline, baseline)).fetchall()
    assert compare_rows(membership.name, membership.columns, duplicate, duplicate)[
        "failure"
    ] == "non_unique_business_identity"
    conn.execute("DELETE FROM turn_events WHERE detect_id=%s", (baseline,))
    conn.execute("INSERT INTO turn_events VALUES (%s,100,10),(%s,101,10)",
                 (baseline, baseline))
    conn.execute("INSERT INTO candidate_intersections(id,detect_id,centroid_x,centroid_y,"
                 "centroid_lon,centroid_lat,radius_m) VALUES (12,%s,0,0,120,30,1)",
                 (baseline,))
    conn.execute("INSERT INTO resolved_subtrack_points VALUES (102,%s,1,0)", (baseline,))
    conn.execute("INSERT INTO turn_events VALUES (%s,102,12),(%s,100,12)",
                 (baseline, baseline))
    shared = conn.execute(membership.query, (baseline, baseline)).fetchall()
    assert compare_rows(membership.name, membership.columns, shared, shared)[
        "failure"
    ] == "non_unique_business_identity"
