"""Opt-in Detect Parquet differential against PostgreSQL Clip readers."""

from __future__ import annotations

import os
import json
import re
from urllib.parse import unquote
from uuid import uuid4

import duckdb
import pytest
from unittest.mock import patch

from track.v2.artifact_parquet import build_descriptor, validate_published
from track.v2.artifact_publication import create_staging, publish
from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.config import PostgresConfig
from track.v2.db.queries import fetch_intersections, fetch_resolved_clip_tracks
from track.v2.db.queries import fetch_detect_row
from track.v2.db.artifact_registry import find_detect_artifact
from track.v2.db.pipeline_store import PostgresPipelineStore
from track.v2.detect_parquet import export_detect_parquet
from track.v2.detect_parquet_reader import DetectDatasetReader
from track.v2.clip import extract_clips, extract_clips_for_detect
from track.v2.models import ClipConfig, ClipInput, RuntimeConfig


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


def _ddl(value: str) -> str:
    return re.sub(r"\b(geometry|ST_[a-z_]+)\s*\(", r"public.\1(", value, flags=re.I)


def _config(dsn: str) -> PostgresConfig:
    from psycopg.conninfo import conninfo_to_dict
    values = conninfo_to_dict(dsn)
    return PostgresConfig(
        host=values.get("host", "127.0.0.1"), port=int(values.get("port", 5432)),
        database=values.get("dbname", "postgres"), user=values.get("user", "postgres"),
        password=unquote(values.get("password", "")), sslmode=values.get("sslmode"),
    )


@pytest.fixture
def database():
    import psycopg
    from psycopg import sql
    dsn = os.environ["ROADNET_TEST_DSN"]
    schema = "detect_parquet_test_" + uuid4().hex
    scoped = psycopg.conninfo.make_conninfo(dsn, options=f"-csearch_path={schema},public")
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(scoped) as conn:
            for _name, statement in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                conn.execute(_ddl(statement))
            conn.commit()
            yield conn, _config(dsn), schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_export_matches_clip_readers_with_crossings_between_points(database, tmp_path):
    conn, config, schema = database
    refine_id, detect_id = uuid4(), uuid4()
    conn.execute("CREATE TABLE source_points(id bigint PRIMARY KEY, vehicle_id text NOT NULL, "
                 "timestamp double precision NOT NULL, lon double precision NOT NULL, "
                 "lat double precision NOT NULL, heading double precision NOT NULL, "
                 "speed double precision NOT NULL, extra jsonb NOT NULL, "
                 "geom public.geometry(Point,4326) NOT NULL)")
    conn.execute("INSERT INTO source_points VALUES "
                 "(1,'v',0,120.0,30.0,0,1,'{}',public.ST_SetSRID(public.ST_MakePoint(120,30),4326)),"
                 "(2,'v',1,120.002,30.0,0,1,'{}',public.ST_SetSRID(public.ST_MakePoint(120.002,30),4326))")
    conn.execute("INSERT INTO refine_runs"
                 "(id,source_table,processing_srid,projection_config,status) "
                 "VALUES (%s,%s,32651,'{\"coordinate_semantics_version\":2,"
                 "\"always_xy\":true,\"distance_semantics\":\"projected_euclidean_metre\","
                 "\"input_heading_semantics\":\"clockwise_degrees_from_true_north\","
                 "\"topology_bearing_semantics\":\"clockwise_degrees_from_processing_grid_north\","
                 "\"enforce_area_of_use\":false,\"max_scale_error\":0.05}','running')",
                 (refine_id, f"{schema}.source_points"))
    conn.execute("INSERT INTO detect_runs(id,refine_id,status) VALUES (%s,%s,'running')",
                 (detect_id, refine_id))
    kept = conn.execute("INSERT INTO kept_trajectory_points"
                        "(refine_id,source_row_key,vehicle_id,subtrack_index,sequence_in_subtrack) "
                        "VALUES (%s,'1','v',1,0),(%s,'2','v',1,1) RETURNING id",
                        (refine_id, refine_id)).fetchall()
    candidate = conn.execute("INSERT INTO candidate_intersections"
                             "(detect_id,centroid_x,centroid_y,centroid_lon,centroid_lat,radius_m) "
                             "VALUES (%s,0,0,120.001,30,10) RETURNING id", (detect_id,)).fetchone()[0]
    intersection = conn.execute("INSERT INTO intersections"
        "(detect_id,candidate_intersection_id,observation_count,clip_radius_m,geom) VALUES "
        "(%s,%s,1,10,public.ST_Buffer(public.ST_SetSRID(public.ST_MakePoint(120.001,30),4326),.001)) RETURNING id",
        (detect_id, candidate)).fetchone()[0]
    resolved_kept = conn.execute("INSERT INTO resolved_subtrack_points"
        "(detect_id,subtrack_index,refined_sequence_in_subtrack,sequence_in_subtrack,kept_point_id) "
        "VALUES (%s,1,0,0,%s),(%s,1,1,3,%s) RETURNING id",
        (detect_id, kept[0][0], detect_id, kept[1][0])).fetchall()
    crossings = conn.execute("INSERT INTO candidate_entry_exit_points"
        "(detect_id,candidate_intersection_id,segment_start_point_id,crossing_type,crossing_x,crossing_y,crossing_lon,crossing_lat) VALUES "
        "(%s,%s,%s,1,0,0,120.0005,30),(%s,%s,%s,0,0,0,120.0015,30) RETURNING id",
        (detect_id, candidate, resolved_kept[0][0], detect_id, candidate, resolved_kept[0][0])).fetchall()
    conn.execute("INSERT INTO resolved_subtrack_points"
        "(detect_id,subtrack_index,sequence_in_subtrack,crossing_point_id) VALUES "
        "(%s,1,1,%s),(%s,1,2,%s)",
        (detect_id, crossings[0][0], detect_id, crossings[1][0]))
    conn.execute("INSERT INTO entry_exit_points"
        "(detect_id,intersection_id,entry_bearing,exit_bearing,entry_crossing_id,exit_crossing_id) "
        "VALUES (%s,%s,90,90,%s,%s)",
        (detect_id, intersection, crossings[0][0], crossings[1][0]))
    conn.commit()
    conn.execute("ALTER TABLE refine_runs DISABLE TRIGGER USER")
    conn.execute(
        "UPDATE refine_runs SET status='completed', source_fingerprint=%s::jsonb WHERE id=%s",
        (json.dumps({"version": 1, "row_count": 2, "sha256": "source"}), refine_id),
    )
    conn.execute("ALTER TABLE refine_runs ENABLE TRIGGER USER")
    conn.commit()

    handle = create_staging(tmp_path.resolve(), "detect", detect_id, uuid4())
    descriptor = export_detect_parquet(
        conn, config, handle, RuntimeConfig(duckdb_threads=1), {"version": 1},
    )
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (detect_id,),
    ).fetchone()[0] == "running"
    assert conn.info.transaction_status.name != "IDLE"

    conn.rollback()  # Close the exporter snapshot before the final transaction.
    publication = publish(handle)
    PostgresPipelineStore(conn).complete_detect_with_artifact(str(detect_id), descriptor)
    conn.commit()
    registered = find_detect_artifact(conn.cursor(), detect_id)
    assert registered.state == "present"
    assert registered.descriptor == descriptor
    assert publication.final_path.stat().st_mode & 0o222 == 0
    validate_published(tmp_path.resolve(), descriptor, "present", verify_sha256=True)
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (detect_id,),
    ).fetchone() == ("completed",)

    expected_intersections = fetch_intersections(conn, str(detect_id))
    expected_tracks = list(fetch_resolved_clip_tracks(conn, str(detect_id)))
    with DetectDatasetReader(
        tmp_path, registered.descriptor, registered.state,
        RuntimeConfig(duckdb_threads=1),
    ) as reader:
        assert list(reader.intersections()) == expected_intersections
        assert list(reader.iter_tracks(batch_size=1)) == expected_tracks
    oracle_input = ClipInput(
        tuple(expected_intersections), tuple(expected_tracks),
        fetch_detect_row(conn, str(detect_id)).spatial_context, str(detect_id),
    )
    for include_boundaries in (True, False):
        clip_config = ClipConfig(include_boundary_clips=include_boundaries)
        expected_clip = extract_clips(oracle_input, config=clip_config)
        with (
            patch("track.v2.db.source_table.verify_refine_source_fingerprint",
                  side_effect=AssertionError("legacy source fingerprint called")),
            patch("track.v2.db.queries.fetch_intersections",
                  side_effect=AssertionError("legacy intersection reader called")),
            patch("track.v2.db.queries.fetch_resolved_clip_tracks",
                  side_effect=AssertionError("legacy track reader called")),
        ):
            actual_clip = extract_clips_for_detect(
                conn, str(detect_id), publication_root=tmp_path,
                runtime=RuntimeConfig(duckdb_threads=1), config=clip_config,
            )
        try:
            assert actual_clip.as_dict() == expected_clip.as_dict()
            assert tuple(actual_clip.clips) == expected_clip.clips
            assert actual_clip.report == expected_clip.report
            assert actual_clip.intersections == expected_clip.intersections
        finally:
            actual_clip.clips.close()
    with duckdb.connect() as db:
        root = publication.final_path
        actual_intersections = db.execute(
            "SELECT intersection_id,lon,lat,radius_m FROM read_parquet(?) ORDER BY intersection_id",
            [str(root / "intersections/part-00000.parquet")],
        ).fetchall()
        actual_points = db.execute(
            "SELECT resolved_point_id,lon,lat,crossing_id,intersection_id,crossing_type "
            "FROM read_parquet(?) ORDER BY subtrack_index,sequence_in_subtrack",
            [str(root / "resolved_points/part-00000.parquet")],
        ).fetchall()
    assert actual_intersections == [
        (item.intersection_id, item.lon, item.lat, item.radius_m) for item in expected_intersections
    ]
    assert actual_points == [
        (point.resolved_point_id, point.lon, point.lat, point.crossing_id,
         point.intersection_id, point.crossing_type)
        for track in expected_tracks for point in track.points
    ]
    assert descriptor.detect_id == detect_id


def test_detect_final_transaction_failure_leaves_reconcilable_orphan(database, tmp_path):
    conn, _config_value, schema = database
    refine_id, detect_id = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO refine_runs"
        "(id,source_table,processing_srid,projection_config,status) "
        "VALUES (%s,%s,3857,'{\"coordinate_semantics_version\":2,"
        "\"always_xy\":true,\"distance_semantics\":\"projected_euclidean_metre\","
        "\"input_heading_semantics\":\"clockwise_degrees_from_true_north\","
        "\"topology_bearing_semantics\":\"clockwise_degrees_from_processing_grid_north\","
        "\"enforce_area_of_use\":false,\"max_scale_error\":0.05}','running')",
        (refine_id, f"{schema}.source_points"),
    )
    conn.execute(
        "INSERT INTO detect_runs(id,refine_id,status) VALUES (%s,%s,'running')",
        (detect_id, refine_id),
    )
    conn.commit()
    conn.execute("ALTER TABLE refine_runs DISABLE TRIGGER USER")
    conn.execute(
        "UPDATE refine_runs SET status='completed', source_fingerprint=%s::jsonb WHERE id=%s",
        (json.dumps({"version": 1, "row_count": 0, "sha256": "0"}), refine_id),
    )
    conn.execute("ALTER TABLE refine_runs ENABLE TRIGGER USER")
    conn.commit()
    handle = create_staging(tmp_path.resolve(), "detect", detect_id, uuid4())
    output = handle.path / "part-00000.parquet"
    with duckdb.connect() as db:
        db.execute("COPY (SELECT 1 AS value) TO ? (FORMAT PARQUET)", [str(output)])
    descriptor = build_descriptor(
        handle, {"probe": [output.name]}, {"version": 1, "row_count": 0, "sha256": "0"},
    )
    publication = publish(handle)
    PostgresPipelineStore(conn).complete_detect_with_artifact(str(detect_id), descriptor)
    conn.rollback()

    assert publication.final_path.is_dir()
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE detect_id=%s", (detect_id,),
    ).fetchone() == (0,)
    store = PostgresPipelineStore(conn)
    store.finish_detect(str(detect_id), status="failed")
    store.commit()
    assert conn.execute(
        "SELECT status FROM detect_runs WHERE id=%s", (detect_id,),
    ).fetchone() == ("failed",)
    validate_published(tmp_path.resolve(), descriptor, "present", verify_sha256=True)
