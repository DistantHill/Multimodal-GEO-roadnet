"""Opt-in Refine publication checks against real PostgreSQL/PostGIS and DuckDB."""

from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, pipeline_config, postgres_config, projection_config, refine_config, runtime_config)

import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote
from uuid import UUID, uuid4

import duckdb
import pytest

from track.v2.artifacts.parquet_manifest import validate_published
from track.v2.artifacts.publish_dataset import abandon_staging, create_staging, publish
from track.v2.cli.init_db import DDL_STATEMENTS, INDEX_STATEMENTS, POST_DDL_STATEMENTS
from track.v2.config import PostgresConfig
from track.v2.db.pipeline_store import PostgresPipelineStore
from track.v2.trajectories.query_refine_points import _stream_vehicle_tracks_from_db
from track.v2.db.source_table import calculate_source_fingerprint, validate_source_table
from track.v2.models import RuntimeConfig
from track.v2.projection import spatial_context_from_values
from track.v2.trajectories.results.export_to_parquet import export_refine_parquet
from track.v2.trajectories.results.parquet_schema import REFINE_PARQUET_V1
from track.v2.trajectories.results.read_parquet import RefineDatasetReader


pytestmark = pytest.mark.skipif(
    not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set",
)


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))


def _postgres_ddl(ddl: str) -> str:
    return re.sub(r"\b(geometry|ST_[a-z_]+)\s*\(", r"public.\1(", ddl, flags=re.I)


def _postgres_config(dsn: str, schema: str) -> PostgresConfig:
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(dsn)
    return postgres_config(
        host=values.get("host", "127.0.0.1"),
        port=int(values.get("port", 5432)),
        database=values.get("dbname", "postgres"),
        user=values.get("user", "postgres"),
        password=unquote(values.get("password", "")),
        connect_timeout=int(values.get("connect_timeout", 10)),
        sslmode=values.get("sslmode"),
    )


@pytest.fixture
def refine_database():
    import psycopg
    from psycopg import sql

    dsn = os.environ["ROADNET_TEST_DSN"]
    schema = "refine_parquet_test_" + uuid4().hex
    setup_dsn = psycopg.conninfo.make_conninfo(
        dsn, options=f"-csearch_path={schema},public",
    )
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(setup_dsn) as conn:
            for _name, ddl in (*DDL_STATEMENTS, *POST_DDL_STATEMENTS, *INDEX_STATEMENTS):
                conn.execute(_postgres_ddl(ddl))
            conn.execute(
                "INSERT INTO pipeline_schema_versions(component, version) VALUES ('track.v2', 5)"
            )
            conn.commit()
            yield conn, _postgres_config(dsn, schema), schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _seed(
    conn, schema: str, *, commit_results: bool = True,
) -> tuple[UUID, dict[str, object]]:
    run_id = uuid4()
    source_table = f"{schema}.source_points"
    conn.execute("""
        CREATE TABLE source_points (
            id bigint PRIMARY KEY, vehicle_id text NOT NULL,
            timestamp double precision NOT NULL, lon double precision NOT NULL,
            lat double precision NOT NULL, heading double precision NOT NULL,
            speed double precision NOT NULL, extra jsonb NOT NULL DEFAULT '{}',
            geom public.geometry(Point, 4326) NOT NULL
        )
    """)
    conn.commit()
    conn.execute("""
        INSERT INTO source_points VALUES
          (10, 'vehicle-a', 1000, 113.9000, 22.5000, 10, 5, '{"source":"a"}',
           public.ST_SetSRID(public.ST_MakePoint(1, 1), 4326)),
          (20, 'vehicle-a', 1002, 113.9002, 22.5002, 20, 6, '{"source":"b"}',
           public.ST_SetSRID(public.ST_MakePoint(2, 2), 4326))
    """)
    projection = {
        "coordinate_semantics_version": 2,
        "always_xy": True,
        "distance_semantics": "projected_euclidean_metre",
        "input_heading_semantics": "clockwise_degrees_from_true_north",
        "topology_bearing_semantics": "clockwise_degrees_from_processing_grid_north",
        "enforce_area_of_use": False,
        "max_scale_error": 0.05,
    }
    conn.execute(
        "INSERT INTO refine_runs(id, source_table, processing_srid, projection_config, status) "
        "VALUES (%s, %s, 32650, %s, 'running')",
        (run_id, source_table, json.dumps(projection)),
    )
    conn.execute(
        "INSERT INTO kept_trajectory_points"
        "(refine_id, source_row_key, vehicle_id, subtrack_index, sequence_in_subtrack) "
        "VALUES (%s, '10', 'vehicle-a', 1, 0), (%s, '20', 'vehicle-a', 1, 1)",
        (run_id, run_id),
    )
    conn.execute("""
        INSERT INTO injected_points(
            refine_id, vehicle_id, subtrack_index, after_sequence_in_subtrack,
            timestamp, lon, lat, x, y, heading, speed, generated_reason
        ) VALUES (%s, 'vehicle-a', 1, 0, 1001, 113.9001, 22.5001,
                  999999, 888888, 15, 5.5, 'orthogonal_heading')
    """, (run_id,))
    if commit_results:
        conn.commit()
    contract = validate_source_table(conn, source_table)
    fingerprint = calculate_source_fingerprint(conn, str(run_id), contract)
    if commit_results:
        conn.rollback()
    return run_id, fingerprint


def _export(conn, config, root: Path, run_id: UUID, fingerprint: dict[str, object]):
    handle = create_staging(root.resolve(), "refine", run_id, uuid4())
    descriptor = export_refine_parquet(
        conn, config, handle, runtime_config(duckdb_threads=1), fingerprint,
        data_root=root.resolve(),
        postgres_extension_path=pipeline_config().paths.duckdb_postgres_extension_path,
    )
    return handle, descriptor


def _read(path: Path, table: str) -> list[dict[str, object]]:
    with duckdb.connect() as db:
        cursor = db.execute(
            f"SELECT * FROM read_parquet(?) ORDER BY refined_sequence_in_subtrack",
            [str(path / table / "part-00000.parquet")],
        )
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _assert_rows(actual: list[dict[str, object]], expected: list[dict[str, object]]) -> None:
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        assert got.keys() == want.keys()
        for name in got:
            if isinstance(want[name], float):
                assert math.isfinite(got[name])
                assert got[name] == pytest.approx(want[name], rel=1e-9, abs=1e-6)
            else:
                assert got[name] == want[name]


def _make_writable(path: Path) -> None:
    for current, directories, files in os.walk(path):
        Path(current).chmod(0o755)
        for name in directories:
            (Path(current) / name).chmod(0o755)
        for name in files:
            (Path(current) / name).chmod(0o644)


def test_real_refine_export_publish_register_and_complete(refine_database, tmp_path) -> None:
    conn, config, schema = refine_database
    run_id, fingerprint = _seed(conn, schema)
    handle, descriptor = _export(conn, config, tmp_path, run_id, fingerprint)
    result = publish(handle)
    store = PostgresPipelineStore(conn)
    store.complete_refine_with_artifact(str(run_id), descriptor, fingerprint)
    store.commit()

    expected_kept = conn.execute("""
        SELECT k.refine_id, k.id, k.vehicle_id, k.subtrack_index,
               (k.sequence_in_subtrack + k.sequence_in_subtrack)::bigint,
               s.timestamp, s.lon, s.lat,
               public.ST_X(public.ST_Transform(
                 public.ST_SetSRID(public.ST_MakePoint(s.lon, s.lat),4326),32650)),
               public.ST_Y(public.ST_Transform(
                 public.ST_SetSRID(public.ST_MakePoint(s.lon, s.lat),4326),32650)),
               s.heading, s.speed, k.source_row_key::text
          FROM kept_trajectory_points k JOIN source_points s ON s.id = k.source_row_key
         WHERE k.refine_id = %s ORDER BY k.sequence_in_subtrack
    """, (run_id,)).fetchall()
    kept_names = [column.name for column in REFINE_PARQUET_V1["kept_points"]]
    expected_kept_dicts = [dict(zip(kept_names, row, strict=True)) for row in expected_kept]
    injected = conn.execute("""
        SELECT refine_id, id, vehicle_id, subtrack_index, 1::bigint, timestamp, lon, lat,
               x, y, heading, speed, generated_reason, after_sequence_in_subtrack
          FROM injected_points WHERE refine_id = %s
    """, (run_id,)).fetchall()
    injected_names = [column.name for column in REFINE_PARQUET_V1["injected_points"]]
    actual_kept = _read(result.final_path, "kept_points")
    _assert_rows(actual_kept, expected_kept_dicts)
    _assert_rows(
        _read(result.final_path, "injected_points"),
        [dict(zip(injected_names, row, strict=True)) for row in injected],
    )
    geom_x = conn.execute(
        "SELECT public.ST_X(public.ST_Transform(geom, 32650)) FROM source_points WHERE id=10"
    ).fetchone()[0]
    assert actual_kept[0]["x"] != pytest.approx(geom_x, rel=1e-9, abs=1e-6)
    assert stat.S_IMODE(result.final_path.stat().st_mode) == 0o555
    assert conn.execute(
        "SELECT status, source_fingerprint = %s::jsonb FROM refine_runs WHERE id = %s",
        (json.dumps(fingerprint), run_id),
    ).fetchone() == ("completed", True)
    assert conn.execute(
        "SELECT a.relative_path, e.state FROM pipeline_artifacts a "
        "JOIN pipeline_artifact_events e ON e.artifact_id=a.id WHERE a.refine_id=%s",
        (run_id,),
    ).fetchone() == (f"refine/{run_id}", "present")
    validate_published(tmp_path, descriptor, "present", verify_sha256=True)
    _make_writable(result.final_path)


def test_real_reader_preserves_source_and_injected_identity_columns(refine_database, tmp_path) -> None:
    conn, config, schema = refine_database
    run_id, fingerprint = _seed(conn, schema)
    conn.execute(
        "UPDATE source_points SET geom=public.ST_SetSRID(public.ST_MakePoint(lon,lat),4326)"
    )
    conn.commit()
    fingerprint = calculate_source_fingerprint(
        conn, str(run_id), validate_source_table(conn, f"{schema}.source_points"),
    )
    handle, descriptor = _export(conn, config, tmp_path, run_id, fingerprint)
    publish(handle)
    source_table = f"{schema}.source_points"
    spatial = spatial_context_from_values(4326, 32650, {
        "coordinate_semantics_version": 2, "always_xy": True,
        "distance_semantics": "projected_euclidean_metre",
        "input_heading_semantics": "clockwise_degrees_from_true_north",
        "topology_bearing_semantics": "clockwise_degrees_from_processing_grid_north",
        "enforce_area_of_use": False, "max_scale_error": 0.05,
    })
    with RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        tracks = list(reader.iter_tracks(source_table, batch_size=2))
    assert len(tracks) == 1
    track = tracks[0]
    assert track.vehicle_id == "vehicle-a"
    assert track.subtrack_index == 1
    assert track.kind.tolist() == ["p", "i", "p"]
    assert track.source_row_key[track.source_row_key_valid].tolist() == [10, 20]
    assert track.timestamp.tolist() == [1000., 1001., 1002.]
    assert track.after_sequence.tolist() == [-1, 0, -1]
    assert track.refine_point_id_valid.all()
    assert not track.xy.flags.writeable
    assert track.xy.shape == (3, 2)
    assert track.generated_reason[1] == "orthogonal_heading"
    _make_writable(handle.final_path)


def test_exporter_independent_connection_cannot_see_uncommitted_rows(
    refine_database, tmp_path,
) -> None:
    conn, config, schema = refine_database
    run_id, fingerprint = _seed(conn, schema, commit_results=False)
    handle, descriptor = _export(conn, config, tmp_path, run_id, fingerprint)
    manifest = {table["name"]: table for table in json.loads(descriptor.manifest_json)["tables"]}

    assert fingerprint["row_count"] == 2
    assert manifest["kept_points"]["row_count"] == 0
    assert manifest["injected_points"]["row_count"] == 0
    assert abandon_staging(handle)
    conn.rollback()


def test_source_change_abandons_staging_without_registry_or_completion(
    refine_database, tmp_path,
) -> None:
    conn, config, schema = refine_database
    run_id, fingerprint = _seed(conn, schema)
    handle, _descriptor = _export(conn, config, tmp_path, run_id, fingerprint)
    conn.execute("UPDATE source_points SET speed = speed + 1 WHERE id = 10")
    conn.commit()
    changed = calculate_source_fingerprint(
        conn, str(run_id), validate_source_table(conn, f"{schema}.source_points"),
    )
    assert changed != fingerprint
    assert abandon_staging(handle)
    assert not handle.final_path.exists()
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (run_id,),
    ).fetchone() == ("running",)
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE refine_id=%s", (run_id,),
    ).fetchone() == (0,)


def test_post_publish_transaction_rollback_keeps_orphan(refine_database, tmp_path) -> None:
    conn, config, schema = refine_database
    run_id, fingerprint = _seed(conn, schema)
    handle, descriptor = _export(conn, config, tmp_path, run_id, fingerprint)
    result = publish(handle)
    PostgresPipelineStore(conn).complete_refine_with_artifact(
        str(run_id), descriptor, fingerprint,
    )
    conn.rollback()  # Controlled failure after the irreversible atomic rename.
    assert result.final_path.is_dir()
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (run_id,),
    ).fetchone() == ("running",)
    assert conn.execute(
        "SELECT count(*) FROM pipeline_artifacts WHERE refine_id=%s", (run_id,),
    ).fetchone() == (0,)
    store = PostgresPipelineStore(conn)
    store.finish_refine(str(run_id), status="failed")
    store.commit()
    assert conn.execute(
        "SELECT status FROM refine_runs WHERE id=%s", (run_id,),
    ).fetchone() == ("failed",)
    validate_published(tmp_path, descriptor, "present", verify_sha256=True)
    _make_writable(result.final_path)
