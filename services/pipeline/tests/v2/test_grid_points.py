import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2.db import grid_points
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, query, params):
        self.query, self.params = query, params

    def fetchone(self):
        return ("trajectory_points", 32650)

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


def test_fetch_points_in_grid_uses_closed_xy_and_bounded_column_batches(monkeypatch):
    metadata = _Cursor([])
    def point(anchor, kind, point_id, source_key, x, sequence):
        return (anchor, kind, point_id, source_key, x, 0.0, 90.0,
                1.0, 2.0, 110.0, 20.0, None, sequence, "trajectory_points")

    missing = (None,) * 14
    data = _Cursor([
        ("car", 1) + point(0, 0, 11, "8", 0.0, 0) + missing
        + point(1, 0, 12, "9", 1.0, 1),
        ("car", 1) + point(1, 0, 12, "9", 1.0, 1)
        + point(0, 0, 11, "8", 0.0, 0) + missing,
    ])
    conn = SimpleNamespace(cursor=lambda **kwargs: data if kwargs else metadata)
    monkeypatch.setattr(grid_points, "validate_source_table", lambda *_args:
        SimpleNamespace(source_table="trajectory_points",
                        quoted_table='"trajectory_points"', key_sql_type="bigint"))
    monkeypatch.setattr(grid_points, "CoordinateTransformer", lambda *_args:
        SimpleNamespace(inverse_bounds=lambda _bounds: (110.0, 20.0, 111.0, 21.0)))

    batches = list(grid_points.fetch_points_in_grid(
        conn, (0, 0), [[(0.0, 0.0, 1.0, 1.0)]], uuid4(),
        SimpleNamespace(processing_srid=32650), batch_size=1,
    ))

    assert len(batches) == 2
    assert batches[0]["point_id"] == (11,)
    assert batches[0]["refined_sequence_in_subtrack"] == (0,)
    assert batches[0]["next_point_id"] == (12,)
    assert batches[0]["next_refined_sequence_in_subtrack"] == (1,)
    assert batches[0]["next_source_id"] == ("trajectory_points",)
    assert batches[1]["prev_point_id"] == (11,)
    # Q89 uses a regular statement to materialize the grid hits, then a
    # server-side cursor only for the bounded neighbour lookups.
    assert metadata.query.startswith("CREATE TEMP TABLE cross_track_hits_")
    assert "ON COMMIT DROP AS" in metadata.query
    assert metadata.query.count("%s") == len(metadata.params)
    assert "ST_X(ST_Transform(s.geom, 32650)) >= %s" in metadata.query
    assert "i.x <= %s" in metadata.query
    assert metadata.params[1:5] == (110.0, 20.0, 111.0, 21.0)
    assert "s.id = k.source_row_key" in metadata.query
    assert data.query.count("%s") == len(data.params) == 4
    assert "FROM cross_track_hits_" in data.query


def test_fetch_points_in_grid_rejects_negative_or_out_of_range_index():
    for index in ((-1, 0), (0, -1), (1, 0), (0, 1)):
        try:
            list(grid_points.fetch_points_in_grid(
                None, index, [[(0.0, 0.0, 1.0, 1.0)]], uuid4(), None,
            ))
        except IndexError:
            pass
        else:
            raise AssertionError(f"accepted grid index {index}")


@pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")
def test_fetch_points_in_grid_postgis_closed_boundary_neighbours_and_batches():
    import psycopg
    from psycopg import sql

    dsn = os.environ["ROADNET_TEST_DSN"]
    schema = "grid_points_test_" + uuid4().hex
    refine_id = uuid4()
    other_refine_id = uuid4()
    context = SpatialContext(
        4326, 32650,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": False,
         "max_scale_error": 0.01},
    )
    transformer = CoordinateTransformer(context)
    positions = [transformer.inverse(400000.0 + offset, 3400000.0)
                 for offset in (0.0, 100.0, 200.0)]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        setup_dsn = psycopg.conninfo.make_conninfo(
            dsn, options=f"-csearch_path={schema},public",
        )
        with psycopg.connect(setup_dsn) as conn:
            conn.execute("""
                CREATE TABLE refine_runs (
                    id uuid PRIMARY KEY, source_table text NOT NULL,
                    processing_srid integer NOT NULL
                );
                CREATE TABLE source_points (
                    id bigint PRIMARY KEY, vehicle_id text NOT NULL,
                    timestamp double precision NOT NULL,
                    lon double precision NOT NULL, lat double precision NOT NULL,
                    heading double precision NOT NULL, speed double precision NOT NULL,
                    geom geometry(Point, 4326) GENERATED ALWAYS AS (
                        ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                    ) STORED
                );
                CREATE INDEX source_points_geom_idx ON source_points USING gist (geom);
                CREATE TABLE kept_trajectory_points (
                    id bigint PRIMARY KEY, refine_id uuid NOT NULL,
                    source_row_key bigint NOT NULL, vehicle_id text NOT NULL,
                    subtrack_index integer NOT NULL,
                    sequence_in_subtrack integer NOT NULL
                );
                CREATE INDEX kept_sequence_idx ON kept_trajectory_points
                    (refine_id, subtrack_index, sequence_in_subtrack);
                CREATE TABLE injected_points (
                    id bigint PRIMARY KEY, refine_id uuid NOT NULL,
                    vehicle_id text NOT NULL, subtrack_index integer NOT NULL,
                    after_sequence_in_subtrack integer NOT NULL,
                    timestamp double precision NOT NULL,
                    lon double precision NOT NULL, lat double precision NOT NULL,
                    x double precision NOT NULL, y double precision NOT NULL,
                    heading double precision NOT NULL, speed double precision NOT NULL,
                    generated_reason text NOT NULL,
                    geom geometry(Point, 4326) GENERATED ALWAYS AS (
                        ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                    ) STORED
                );
                CREATE INDEX injected_geom_idx ON injected_points USING gist (geom);
                CREATE INDEX injected_sequence_idx ON injected_points
                    (refine_id, subtrack_index, after_sequence_in_subtrack)
            """)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO refine_runs VALUES (%s, %s, 32650)",
                    [(refine_id, f"{schema}.source_points"),
                     (other_refine_id, f"{schema}.source_points")],
                )
                cur.executemany(
                    "INSERT INTO source_points "
                    "(id, vehicle_id, timestamp, lon, lat, heading, speed) "
                    "VALUES (%s, 'car', %s, %s, %s, 90, 5)",
                    [(id_, float(id_), lon, lat)
                     for id_, (lon, lat) in enumerate(positions, start=1)],
                )
                cur.executemany(
                    "INSERT INTO kept_trajectory_points VALUES "
                    "(%s, %s, %s, 'car', 1, %s)",
                    [(100 + seq, refine_id, str(seq + 1), seq)
                     for seq in range(3)]
                    + [(200, other_refine_id, '2', 0)],
                )
            boundary_x, boundary_y = conn.execute(
                "SELECT ST_X(ST_Transform(geom, 32650)), "
                "ST_Y(ST_Transform(geom, 32650)) "
                "FROM source_points WHERE id = 2"
            ).fetchone()
            first_x, first_y = conn.execute(
                "SELECT ST_X(ST_Transform(geom, 32650)), "
                "ST_Y(ST_Transform(geom, 32650)) "
                "FROM source_points WHERE id = 1"
            ).fetchone()
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO injected_points "
                    "(id, refine_id, vehicle_id, subtrack_index, "
                    "after_sequence_in_subtrack, timestamp, lon, lat, x, y, "
                    "heading, speed, generated_reason) "
                    "VALUES (%s, %s, 'car', 1, %s, %s, %s, %s, %s, %s, 90, 5, 'test')",
                    [(300, refine_id, 0, 0.5, *positions[0], first_x, first_y),
                     (301, refine_id, 1, 1.5, *positions[1], boundary_x, boundary_y),
                     (302, other_refine_id, 0, 0.5, *positions[1], boundary_x, boundary_y)],
                )
            conn.commit()
            grids = [
                [(boundary_x, boundary_y - 1.0,
                  boundary_x + 1.0, boundary_y + 1.0)],
                [(boundary_x - 1.0, boundary_y - 1.0,
                  boundary_x, boundary_y + 1.0)],
            ]
            batches = list(grid_points.fetch_points_in_grid(
                conn, (0, 0), grids, refine_id, context, batch_size=1,
            ))
            left_batches = list(grid_points.fetch_points_in_grid(
                conn, (1, 0), grids, refine_id, context, batch_size=1,
            ))
            assert [batch["point_id"][0] for batch in batches] == [101, 301]
            assert [batch["point_id"][0] for batch in left_batches] == [101, 301]
            assert [
                batch["refined_sequence_in_subtrack"][0] for batch in batches
            ] == [2, 3]
            assert batches[0]["prev_point_id"] == (300,)
            assert batches[0]["next_point_id"] == (301,)
            assert batches[1]["prev_point_id"] == (101,)
            assert batches[1]["next_point_id"] == (102,)
            assert batches[1]["prev_refined_sequence_in_subtrack"] == (2,)
            assert batches[1]["next_refined_sequence_in_subtrack"] == (4,)
            assert batches[1]["generated_reason"] == ("test",)
            assert batches[1]["source_id"] == ("public.injected_points",)
            assert all(len(batch["point_id"]) == 1 for batch in batches)
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            ))
