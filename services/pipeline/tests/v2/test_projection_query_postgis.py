"""Opt-in PostGIS acceptance checks; uses only transaction-local temp objects."""

from __future__ import annotations

import os
import math
from dataclasses import replace

import pytest

from track.v2.candidates.query_source_windows import _query_active_points_in_window
from track.v2.models import SpatialContext
from track.v2.projection import CoordinateTransformer, QueryWindowRequest, build_query_window


@pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")
def test_postgis_prefilter_keeps_known_members_and_returns_run_xy() -> None:
    import psycopg

    run_context = SpatialContext(
        4326, 32649,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": False, "max_scale_error": .01},
    )
    run_transformer = CoordinateTransformer(run_context)
    window = build_query_window(QueryWindowRequest(113.99, 39.0, 5000.0, 3000.0))
    query_context = SpatialContext(
        4326, window.query_srid,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": False, "max_scale_error": .015},
    )
    query_transformer = CoordinateTransformer(query_context)
    inner_lon, inner_lat = query_transformer.inverse(
        (window.x_min + window.x_max) / 2.0,
        (window.y_min + window.y_max) / 2.0,
    )
    outside_lon, outside_lat = query_transformer.inverse(
        window.x_max + .01, (window.y_min + window.y_max) / 2.0,
    )
    injected_x, injected_y = run_transformer.forward(inner_lon, inner_lat)

    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE projection_raw (
                    id bigint PRIMARY KEY, vehicle_id text NOT NULL, timestamp double precision NOT NULL,
                    lon double precision NOT NULL, lat double precision NOT NULL,
                    heading double precision NOT NULL, speed double precision NOT NULL,
                    extra jsonb NOT NULL DEFAULT '{}'::jsonb,
                    geom geometry(Point, 4326) NOT NULL
                ) ON COMMIT DROP;
                CREATE INDEX ON projection_raw USING gist (geom);
                CREATE TEMP TABLE kept_trajectory_points (
                    id bigint PRIMARY KEY, refine_id text NOT NULL, vehicle_id text NOT NULL,
                    source_row_key bigint NOT NULL, subtrack_index integer NOT NULL,
                    sequence_in_subtrack integer NOT NULL
                ) ON COMMIT DROP;
                CREATE TEMP TABLE injected_points (
                    id bigint PRIMARY KEY, refine_id text NOT NULL, vehicle_id text NOT NULL,
                    timestamp double precision NOT NULL, lon double precision NOT NULL,
                    lat double precision NOT NULL, heading double precision NOT NULL,
                    speed double precision NOT NULL, generated_reason text NOT NULL,
                    x double precision NOT NULL, y double precision NOT NULL,
                    geom geometry(Point, 4326) NOT NULL, subtrack_index integer NOT NULL,
                    after_sequence_in_subtrack integer NOT NULL
                ) ON COMMIT DROP;
                CREATE INDEX ON injected_points USING gist (geom)
            """)
            cur.execute(
                "INSERT INTO projection_raw VALUES "
                "(%s, 'raw-inner', 1, %s, %s, 0, 1, '{}', ST_SetSRID(ST_Point(%s,%s),4326)), "
                "(%s, 'raw-outside', 2, %s, %s, 0, 1, '{}', ST_SetSRID(ST_Point(%s,%s),4326))",
                (1, inner_lon, inner_lat, inner_lon, inner_lat,
                 2, outside_lon, outside_lat, outside_lon, outside_lat),
            )
            cur.execute(
                "INSERT INTO kept_trajectory_points VALUES "
                "(11, 'refine-1', 'raw-inner', '1', 1, 0), "
                "(12, 'refine-1', 'raw-outside', '2', 2, 0)"
            )
            cur.execute(
                "INSERT INTO injected_points VALUES "
                "(21, 'refine-1', 'injected-inner', 1, %s, %s, 0, 1, 'fixture', "
                "%s, %s, ST_SetSRID(ST_Point(%s,%s),4326), 3, 0)",
                (inner_lon, inner_lat, injected_x, injected_y, inner_lon, inner_lat),
            )

        points = _query_active_points_in_window(
            conn, window, table="projection_raw", kept_table="kept_trajectory_points",
            source_table="projection_raw", refine_id="refine-1", spatial_context=run_context,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id::text FROM projection_raw WHERE "
                f"ST_X(ST_Transform(geom,{window.query_srid})) BETWEEN %s AND %s AND "
                f"ST_Y(ST_Transform(geom,{window.query_srid})) BETWEEN %s AND %s ORDER BY id",
                (window.x_min, window.x_max, window.y_min, window.y_max),
            )
            oracle_raw = [row[0] for row in cur]

            # Build a window from the values PostgreSQL actually compares.  The
            # first point is exactly on the closed lower bound; the second is
            # one representable float above the closed upper bound.
            cur.execute(
                f"SELECT id, ST_X(ST_Transform(geom,{window.query_srid})), "
                f"ST_Y(ST_Transform(geom,{window.query_srid})) "
                "FROM projection_raw ORDER BY id"
            )
            projected = list(cur)
            inner_query_x, inner_query_y = projected[0][1:]
            outside_query_x = projected[1][1]
            adjacent_window = replace(
                window,
                x_min=inner_query_x,
                x_max=math.nextafter(outside_query_x, -math.inf),
                y_min=min(window.y_min, inner_query_y),
                y_max=max(window.y_max, inner_query_y),
            )

        adjacent_points = _query_active_points_in_window(
            conn, adjacent_window, table="projection_raw",
            source_table="projection_raw", spatial_context=run_context,
        )

    raw_ids = [str(key) for track in points for key in track.source_row_key[track.source_row_key_valid]]
    assert raw_ids == oracle_raw == ["1"]
    assert {point.vehicle_id for point in points} == {"raw-inner", "injected-inner"}
    assert [point.vehicle_id for point in adjacent_points] == ["raw-inner"]
    for track in points:
        expected = run_transformer.forward_many(track.lonlat)
        assert track.xy[:, 0] == pytest.approx(expected[:, 0], rel=0, abs=1e-6)
        assert track.xy[:, 1] == pytest.approx(expected[:, 1], rel=0, abs=1e-6)
