"""Real PostgreSQL writer regression for overlapping plugin instance evidence."""
from dataclasses import dataclass
from types import SimpleNamespace as NS

from .test_completion_postgres import connection, seed_detect, pytestmark
from .test_writer_relation import _turns, _crosses, _mappings, _cluster
from track.v2.plugins.execution import CandidateResult, CompositeRelation
from track.v2.db.write_detect_results import (
    _insert_turn_events, _insert_tracks_cross, _insert_candidate_instances,
    materialize_turn_event_geom, materialize_turn_hot_points,
)


@dataclass(frozen=True)
class Params:
    threshold: float


def test_overlapping_instances_write_identity_metadata_and_geometry_mirrors(connection, tmp_path):
    conn = connection
    conn.execute("CREATE TABLE fixture_points(id bigint PRIMARY KEY, geom public.geometry(Point,4326))")
    conn.execute("INSERT INTO fixture_points VALUES (1,public.ST_SetSRID(public.ST_MakePoint(113,23),4326)), (2,public.ST_SetSRID(public.ST_MakePoint(113.001,23),4326))")
    detect_id = str(seed_detect(conn, source_table='fixture_points'))
    turn = _turns([0])
    cross = _crosses([0])
    outputs = (
        CandidateResult('custom_turn', 'low', 'turn', turn, turn, Params(30.), 0),
        CandidateResult('custom_turn', 'high', 'turn', turn, turn, Params(60.), 1),
        CandidateResult('custom_cross', 'one', 'orthogonal_cross_track', cross, cross, Params(1.), 0),
        CandidateResult('custom_cross', 'two', 'orthogonal_cross_track', cross, cross, Params(2.), 1),
    )
    result = NS(candidate_results=outputs,
                turn_events=CompositeRelation(((turn,0),(turn,1))),
                cross_track_candidates=CompositeRelation(((cross,0),(cross,1))))
    # This regression consumes already resolved source rows, exercising production
    # COPY, candidate identity adaptation, metadata and both geometry mirrors.
    from track.v2.intersections.relations.source_associations import WriterRelation
    from .config_factory import runtime_config
    with WriterRelation(runtime_config(), tmp_path, detect_id, batch_size=1) as relation:
        relation.collect_sources(result, [], {}, {})
        _mappings(relation, [('p',1,0),('p',2,0)], [1,2])
        relation.resolve_sources()
        cluster = _cluster(0, [item.source_key for item in outputs], [0,1,0,1])
        relation.prepare_annotations([cluster], {0:1}, result.turn_events)
        assert _insert_turn_events(conn, detect_id, relation, batch_size=1) == 2
        assert _insert_tracks_cross(conn, detect_id, relation, batch_size=1) == 2
        _insert_candidate_instances(conn, detect_id, result)
        assert materialize_turn_event_geom(conn, detect_id) == 2
        assert materialize_turn_hot_points(conn, detect_id) == 2
    for table in ('turn_events','turn_events_geom','trun_hot_ponits','tracks_cross'):
        rows = conn.execute(f"SELECT algorithm_id,instance_id,local_candidate_id,evidence_index FROM {table} WHERE detect_id=%s ORDER BY evidence_index", (detect_id,)).fetchall()
        expected = [('custom_cross','one',0,0),('custom_cross','two',0,1)] if table == 'tracks_cross' else [('custom_turn','low',0,0),('custom_turn','high',0,1)]
        assert rows == expected
    instances = conn.execute("SELECT algorithm_id,instance_id,params,evidence_count,candidate_count FROM detect_candidate_instances WHERE detect_id=%s ORDER BY algorithm_id,instance_id", (detect_id,)).fetchall()
    assert len(instances) == 4
    assert {row[1]: row[2]['threshold'] for row in instances} == {'low':30.,'high':60.,'one':1.,'two':2.}
    assert all(row[-2:] == (1,1) for row in instances)
