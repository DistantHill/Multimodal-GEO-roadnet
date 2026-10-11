"""Opt-in real PostGIS writer parity and independent archive transactions."""
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import psycopg
import pytest
from shapely.geometry import box, mapping

from pipeline.experiments.archive import ArchiveStore
from track.v2.db.write_detect_results import _insert_intersections
from track.v2.experiments.intersection_hpo.local_adapter import final_faces, RULES
from test_experiment_archive_integration import store, record


CREATE_FIXTURE_SQL = """CREATE TEMP TABLE intersections (
    id bigint PRIMARY KEY, detect_id uuid, candidate_intersection_id bigint,
    branch_centers_deg jsonb, transition_counts jsonb, observation_count integer,
    clip_radius_m double precision, geom geometry(Polygon,4326))"""
WRITER_FACE_SQL = "SELECT ST_AsGeoJSON(geom),ST_NPoints(geom) FROM intersections WHERE id=%s"
ARCHIVE_EXISTS_SQL = "SELECT id FROM eval_experiments WHERE id=%s"


def test_postgis_final_face_matches_writer_and_archive_commits_are_independent(store):
    connection = store.connection
    connection.execute(CREATE_FIXTURE_SQL)
    cluster = NS(cluster_calculate_index=3, centroid_x=335000., centroid_y=3845000., radius_m=20.)
    topology = NS(cluster_calculate_index=3, branch_centers_deg=[0., 90., 180.],
                  transition_counts={(0, 1): 3}, observation_count=3,
                  is_complete=True, clip_radius_m=35.)
    spatial = NS(processing_srid=32649)
    _insert_intersections(connection, str(uuid4()), [topology], (1,), {3: 2}, {3: cluster}, spatial, 10)
    written_geometry, vertex_count = connection.execute(WRITER_FACE_SQL, (1,)).fetchone()
    roi = json.loads(json.dumps(mapping(box(-180, -90, 180, 90))))
    predictions = final_faces(connection, NS(clusters=[cluster], topologies=[topology],
                                             spatial_context=spatial), roi)
    assert predictions["features"][0]["geometry"] == json.loads(written_geometry)
    assert vertex_count == 33
    connection.commit()

    first, second = record(), record()
    first["case_snapshot"]["geometry"] = roi
    with connection.transaction():
        store.save_experiment(first, predictions)
        evaluated = store.append_evaluation(first["id"], predictions, RULES)
    assert evaluated["metrics"]["tp"] == 1
    assert evaluated["metrics"]["f1"] == 1.
    with psycopg.connect(connection.info.dsn, password=connection.info.password) as observer:
        assert observer.execute(ARCHIVE_EXISTS_SQL, (first["id"],)).fetchone() is not None

    with pytest.raises(ValueError):
        with connection.transaction():
            store.save_experiment(second, predictions)
            store.append_evaluation(second["id"], predictions,
                                    {"mode": "both", "distance_m": 30., "iou_threshold": 2.})
    with psycopg.connect(connection.info.dsn, password=connection.info.password) as observer:
        assert observer.execute(ARCHIVE_EXISTS_SQL, (first["id"],)).fetchone() is not None
        assert observer.execute(ARCHIVE_EXISTS_SQL, (second["id"],)).fetchone() is None
        archived = ArchiveStore(observer).get_experiment(first["id"])
        assert archived["metrics"] == evaluated["metrics"]
