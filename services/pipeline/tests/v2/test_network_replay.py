from __future__ import annotations

import json
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
