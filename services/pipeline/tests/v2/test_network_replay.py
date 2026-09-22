from __future__ import annotations

import json
import os
from unittest.mock import patch
from uuid import uuid4

import pytest

from track.v2.validation import network_replay
from track.v2.validation.network_replay import NetworkReplayError, replay_network


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Cursor:
    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.itersize = 0
        self.rows = []
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params):
        self.connection.queries.append((self.name, query, params))
        self.rows = (self.connection.intersections if "FROM intersections" in query
                     else self.connection.centerlines)

    def fetchmany(self, size):
        result = self.rows[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class _Connection:
    def __init__(self):
        self.status = ("completed", "completed")
        self.intersections = [
            (1, 120.0, 30.0, 10.0),
            (2, 120.001, 30.001, 11.0),
            (3, 120.002, 30.002, 12.0),
        ]
        self.centerlines = [
            (1, 2, '{"type":"LineString","coordinates":[[120,30],[120.001,30.001]]}',
             20.0, 3, 5),
        ]
        self.queries = []

    def execute(self, query, params):
        self.queries.append((None, query, params))
        return _Result(self.status)

    def cursor(self, name=None):
        assert name is not None
        return _Cursor(self, name)


def _replay(connection=None, identities=None):
    return replay_network(
        connection or _Connection(), uuid4(), uuid4(),
        identities or [(1, "z"), (2, "a"), (3, "isolated")], batch_size=1,
    )


def test_replay_reads_each_small_relation_once_and_normalizes_all_outputs() -> None:
    conn = _Connection()
    result = _replay(conn)
    named = [(name, query) for name, query, _params in conn.queries if name]
    assert len(named) == 2
    assert sum("FROM intersections" in query for _name, query in named) == 1
    assert sum("FROM centerlines" in query for _name, query in named) == 1
    assert not any("point" in query.lower() or "manifest" in query.lower()
                   for _name, query in named)
    assert result.evidence["configs"] == [
        {"include_isolated_nodes": False}, {"include_isolated_nodes": True},
    ]
    assert result.evidence["historical_output_reconstructed"] is False
    assert result.evidence["intersection_rows_read"] == 3
    assert result.evidence["centerline_rows_read"] == 1
    false_nodes = result.tables["network.replay.false.nodes"]
    true_nodes = result.tables["network.replay.true.nodes"]
    assert [row[0] for row in false_nodes] == ["a", "z"]
    assert [row[0] for row in true_nodes] == ["a", "isolated", "z"]
    edge = result.tables["network.replay.false.edges"][0]
    assert json.loads(edge[0]) == ["edge", "a", "z"]
    assert json.loads(edge[1])["coordinates"] == [[120.001, 30.001], [120.0, 30.0]]
    assert edge[3:] == (8, 5, 3)
    assert result.tables["network.replay.false.report"] == [("report", 1, 2, 1, 1, 0)]
    assert result.tables["network.replay.true.report"] == [("report", 1, 3, 1, 1, 0)]


@pytest.mark.parametrize(
    "mutation,identities,match",
    [
        (lambda conn: setattr(conn, "status", ("completed", "running")), None, "lineage"),
        (lambda conn: None, [(1, "same"), (2, "same"), (3, "isolated")], "not unique"),
        (lambda conn: None, [(1, "z"), (2, "a")], "incomplete"),
        (lambda conn: conn.intersections.__setitem__(0, (1, float("nan"), 30, 1)),
         None, "geometry"),
        (lambda conn: conn.centerlines.__setitem__(0, (1, 2, "bad", 20, 3, 5)),
         None, "geometry"),
        (lambda conn: conn.centerlines.append(conn.centerlines[0]), None, "identity"),
    ],
)
def test_replay_fails_closed_on_unreliable_contract(mutation, identities, match) -> None:
    conn = _Connection()
    mutation(conn)
    with pytest.raises(NetworkReplayError, match=match):
        _replay(conn, identities)


def test_anchor_mismatch_fails_before_database_reads() -> None:
    conn = _Connection()
    with patch.object(network_replay, "_network_hash", return_value="changed"):
        with pytest.raises(NetworkReplayError, match="anchor mismatch"):
            _replay(conn)
    assert conn.queries == []


def test_new_network_config_field_fails_before_database_reads() -> None:
    conn = _Connection()
    existing = network_replay.fields(network_replay.NetworkConfig)
    with patch.object(network_replay, "fields", return_value=existing + existing):
        with pytest.raises(NetworkReplayError, match="domain changed"):
            _replay(conn)
    assert conn.queries == []


def test_real_postgres_replay_reads_only_persisted_small_inputs() -> None:
    if not os.environ.get("ROADNET_TEST_DSN"):
        pytest.skip("ROADNET_TEST_DSN is not set")
    import psycopg
    from psycopg import sql

    dsn = os.environ["ROADNET_TEST_DSN"]
    schema = "network_replay_" + uuid4().hex
    detect_id, fit_id = uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = psycopg.conninfo.make_conninfo(dsn, options=f"-csearch_path={schema},public")
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute("""
              CREATE TABLE detect_runs(id uuid PRIMARY KEY,status text);
              CREATE TABLE fit_runs(id uuid PRIMARY KEY,detect_id uuid,status text);
              CREATE TABLE candidate_intersections(
                id bigint,detect_id uuid,centroid_lon double precision,centroid_lat double precision);
              CREATE TABLE intersections(
                id bigint,detect_id uuid,candidate_intersection_id bigint,clip_radius_m double precision);
              CREATE TABLE centerlines(
                fit_id uuid,detect_id uuid,variant text,from_intersection_id bigint,
                to_intersection_id bigint,geom public.geometry(LineString,4326),
                length_m double precision,forward_clip_count integer,reverse_clip_count integer)
            """)
            conn.execute("INSERT INTO detect_runs VALUES (%s,'completed')", (detect_id,))
            conn.execute("INSERT INTO fit_runs VALUES (%s,%s,'completed')",
                         (fit_id, detect_id))
            conn.execute("INSERT INTO candidate_intersections VALUES "
                         "(10,%s,120,30),(20,%s,120.001,30.001),(30,%s,121,31)",
                         (detect_id, detect_id, detect_id))
            conn.execute("INSERT INTO intersections VALUES "
                         "(1,%s,10,10),(2,%s,20,11),(3,%s,30,12)",
                         (detect_id, detect_id, detect_id))
            conn.execute("INSERT INTO centerlines VALUES "
                         "(%s,%s,'combined',1,2,"
                         "public.ST_GeomFromText('LINESTRING(120 30,120.001 30.001)',4326),"
                         "20,3,5)", (fit_id, detect_id))
            result = replay_network(
                conn, detect_id, fit_id, [(1, "z"), (2, "a"), (3, "isolated")],
                batch_size=1,
            )
            assert len(result.tables["network.replay.false.nodes"]) == 2
            assert len(result.tables["network.replay.true.nodes"]) == 3
            assert result.evidence["detect_id"] == str(detect_id)
            assert result.evidence["fit_id"] == str(fit_id)
            conn.rollback()
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            ))
