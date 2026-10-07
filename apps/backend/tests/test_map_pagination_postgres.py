"""Page resolved points without changing geometry, vehicle identity or ordering."""

import os
from unittest.mock import Mock

import psycopg
import pytest

from backend.stores.map_layers import PostgresMapStore, _DetectContext, _RefineContext


pytestmark = pytest.mark.skipif(not os.environ.get("ROADNET_TEST_DSN"), reason="ROADNET_TEST_DSN is not set")


def test_resolved_pages_match_full_projection_with_all_source_kinds():
    with psycopg.connect(os.environ["ROADNET_TEST_DSN"]) as connection:
        connection.execute("SET LOCAL search_path TO pg_temp, public, pg_catalog")
        connection.execute("""
            CREATE TEMP TABLE source_points (
                id bigint PRIMARY KEY, lon float8, lat float8, geom geometry(Point,4326));
            CREATE TEMP TABLE kept_trajectory_points (
                id bigint PRIMARY KEY, source_row_key bigint, vehicle_id text, refine_id text);
            CREATE TEMP TABLE injected_points (
                id bigint PRIMARY KEY, vehicle_id text, refine_id text, lon float8, lat float8,
                geom geometry(Point,4326));
            CREATE TEMP TABLE candidate_entry_exit_points (
                id bigint PRIMARY KEY, detect_id text, candidate_intersection_id bigint,
                crossing_lon float8, crossing_lat float8, crossing_type integer, geom geometry(Point,4326));
            CREATE TEMP TABLE intersections (
                id bigint PRIMARY KEY, detect_id text, candidate_intersection_id bigint);
            CREATE TEMP TABLE resolved_subtrack_points (
                id bigint PRIMARY KEY, detect_id text, subtrack_index integer, sequence_in_subtrack integer,
                kept_point_id bigint, injected_point_id bigint, crossing_point_id bigint);
            INSERT INTO source_points VALUES
                (0,121,30,ST_SetSRID(ST_Point(121,30),4326)),
                (-1,120.2,30,ST_SetSRID(ST_Point(120.2,30),4326)),
                (1,122,30,ST_SetSRID(ST_Point(122,30),4326)),
                (2,120,30,NULL);
            INSERT INTO kept_trajectory_points VALUES
                (1,0,'a','refine'), (2,-1,'z','refine'), (3,1,'v2','refine'),
                (4,2,'v3','refine'), (5,0,'other','other');
            INSERT INTO injected_points VALUES
                (10,'z','refine',120.1,30,ST_SetSRID(ST_Point(120.1,30),4326));
            INSERT INTO candidate_entry_exit_points VALUES
                (20,'detect',1,120,30,0,ST_SetSRID(ST_Point(120,30),4326)),
                (21,'detect',2,120,30,1,ST_SetSRID(ST_Point(120,30),4326));
            INSERT INTO intersections VALUES (31,'detect',1),(32,'detect',2),(33,'other',1);
            INSERT INTO resolved_subtrack_points VALUES
                (100,'detect',7,0,NULL,NULL,20), (102,'detect',7,1,NULL,10,NULL),
                (101,'detect',7,2,1,NULL,NULL), (103,'detect',7,3,2,NULL,NULL),
                (200,'detect',8,0,NULL,NULL,21), (201,'detect',8,1,3,NULL,NULL),
                (300,'detect',9,0,4,NULL,NULL), (400,'other',7,0,5,NULL,NULL);
        """)
        refine = _RefineContext("refine", "source_points", '"source_points"', "bigint", {})
        context = _DetectContext("detect", refine, 4326, 3857, {})
        store = PostgresMapStore(connection)
        def geom(lon):
            return {"type": "Point", "coordinates": [lon, 30]}

        reference = [
            (7, "a", 100, None, "crossing", 20, 31, 0, geom(120)),
            (7, "a", 102, None, "injected", None, None, None, geom(120.1)),
            (7, "a", 101, 0, "kept", None, None, None, geom(121)),
            (7, "a", 103, -1, "kept", None, None, None, geom(120.2)),
            (8, "v2", 200, None, "crossing", 21, 32, 1, geom(120)),
            (8, "v2", 201, 1, "kept", None, None, None, geom(122)),
            (9, "v3", 300, 2, "kept", None, None, None, None),
        ]
        assert [row[2] for row in reference] == [100, 102, 101, 103, 200, 201, 300]
        for bbox in (None, (120., 29., 120.2, 31.), (119., 29., 123., 31.), (0., 0., 1., 1.)):
            expected = reference if bbox is None else [
                row for row in reference if row[8] is not None
                and bbox[0] <= row[8]["coordinates"][0] <= bbox[2]
                and bbox[1] <= row[8]["coordinates"][1] <= bbox[3]
            ]
            for limit in (1, 3, 20):
                for offset in (0, 1, 3, 20):
                    assert store._resolved_point_layer(context, bbox, limit, offset) == expected[offset:offset + limit]

        # The crossing's vehicle anchor is outside both its page and this bbox.
        page = store._resolved_point_layer(context, (120., 29., 120.2, 31.), 1)
        assert page[0][1] == "a"
        assert page[0][6] == 31
        store._resolve_detect = Mock(return_value=context)
        first = store.read_layer("resolved_subtrack_points", bbox=(120., 29., 120.2, 31.),
                                 detect_id="detect", fit_id=None, limit=3)
        assert [feature["id"] for feature in first["features"]] == [100, 102, 103]
        assert first["metadata"]["has_more"] is True
        assert first["metadata"]["next_cursor"] == 3
        last = store.read_layer("resolved_subtrack_points", bbox=(120., 29., 120.2, 31.),
                                detect_id="detect", fit_id=None, limit=3, offset=3)
        assert [feature["id"] for feature in last["features"]] == [200]
        assert last["metadata"]["has_more"] is False
        assert last["metadata"]["next_cursor"] is None
        connection.rollback()
