"""Normalized DuckDB TurnEvent relation behavior and corruption guards."""
from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

from track.v2 import turn_event_relation
from track.v2.models import PointRef, RefinedPointRef, RuntimeConfig, TrackModel, TurnEvent
from track.v2.turn_event_relation import TurnCoordinate, TurnEventRelation


def _event(index: int, *, source_ids: tuple[str, ...] = ()) -> TurnEvent:
    return TurnEvent(
        event_calculate_index=index, vehicle_id="vehicle", timestamp=10.5 + index,
        lon=120.0, lat=30.0, x=1.0 + index, y=2.0 + index,
        entry_bearing=20.0, exit_bearing=50.0, turn_deg=30.0, score=30.0,
        source="turn", source_ids=source_ids,
    )


def _full_event(index: int = 0) -> TurnEvent:
    base = _event(index, source_ids=("s2", "s1", "s2"))
    return replace(
        base,
        point_ref=PointRef(
            "source", "row", True, "from", "to", "injected", "hash"
        ),
        refined_point_ref=RefinedPointRef("injected", 9, 2, 4),
        cross_track_calculate_index=7,
        g_star_z=-1.25,
    )


def _relation(tmp_path: Path, *, batch_size: int = 1) -> TurnEventRelation:
    return TurnEventRelation(
        RuntimeConfig(
            duckdb_memory_limit="128MB", duckdb_threads=1,
            duckdb_max_temp_directory_size="256MB",
        ),
        tmp_path,
        batch_size=batch_size,
        relation_id="test-relation",
    )


def test_empty_relation_seals_and_repeats(tmp_path):
    with _relation(tmp_path) as relation:
        relation.seal()
        assert len(relation) == 0
        assert list(relation) == list(relation) == []
        assert list(relation.iter_coordinates()) == []


def test_lossless_optional_source_order_index_and_batch_one(tmp_path):
    events = (_full_event(), _event(1))
    with _relation(tmp_path) as relation:
        for event in events:
            relation.append(event)
        with pytest.raises(RuntimeError, match="not sealed"):
            list(relation)
        relation.seal()
        assert tuple(relation) == tuple(relation) == events
        assert relation[0] == relation[-2] == events[0]
        assert relation[-1] == events[1]
        assert relation[:] == events
        assert relation[::2] == events[::2]
        with pytest.raises(IndexError):
            _ = relation[2]
        assert list(relation.iter_coordinates()) == [
            TurnCoordinate(0, 1.0, 2.0), TurnCoordinate(1, 2.0, 3.0)
        ]
        assert events[0].as_dict() == relation[0].as_dict()
        output = TrackModel.from_dict({"events": relation}).as_dict()
        assert output == {"events": [event.as_dict() for event in events]}
        assert json.loads(json.dumps(output)) == output


@pytest.mark.parametrize("batch_size", [1, 2, 5])
def test_extend_is_lossless_bounded_and_preserves_nullable_bigints(tmp_path, batch_size):
    huge = 2**60 + 7
    events = (
        _event(0),
        replace(_full_event(1), refined_point_ref=RefinedPointRef(
            "kept", huge, huge + 1, huge + 2,
        ), cross_track_calculate_index=huge + 3),
        _event(2, source_ids=("string",)),
    )
    with _relation(tmp_path, batch_size=2) as relation:
        relation.extend(iter(events), batch_size=batch_size)
        assert len(relation) == 3
        relation.seal()
        assert tuple(relation) == events


def test_extend_rolls_back_failed_batch_but_keeps_prior_commit(tmp_path):
    with _relation(tmp_path, batch_size=1) as relation:
        with pytest.raises((TypeError, ValueError, OverflowError)):
            relation.extend((_event(0), replace(_event(1), event_calculate_index="bad")))
        assert len(relation) == 1
        assert relation._connection.execute("SELECT count(*) FROM turn_events").fetchone() == (1,)
        relation.extend((_event(1),))
        relation.seal()
        assert tuple(relation) == (_event(0), _event(1))


def test_extend_null_source_id_follows_seal_rejection(tmp_path):
    with _relation(tmp_path, batch_size=1) as relation:
        relation.extend((_event(0, source_ids=(None,)),))
        with pytest.raises(ValueError, match="invalid TurnEvent relation"):
            relation.seal()


def test_extend_matches_append_bool_to_varchar_coercion(tmp_path):
    with _relation(tmp_path / "append") as appended, _relation(tmp_path / "extend") as extended:
        appended.append(_event(0, source_ids=(True, "string")))
        extended.extend((_event(0, source_ids=(True, "string")),))
        appended.seal()
        extended.seal()
        assert tuple(extended) == tuple(appended)


def test_extend_streams_one_events_source_ids_in_bounded_chunks(tmp_path):
    source_ids = (f"source-{index}" for index in range(7))
    event = replace(_event(0), source_ids=source_ids)
    with _relation(tmp_path, batch_size=2) as relation:
        relation.append_many(iter((event,)), batch_size=1)
        relation.seal()
        assert relation[0].source_ids == tuple(f"source-{index}" for index in range(7))


def test_extend_nan_cannot_bypass_relation_validation(tmp_path):
    relation = _relation(tmp_path)
    relation.extend((replace(_event(0), x=float("nan")),))
    with pytest.raises(ValueError, match="invalid TurnEvent relation"):
        relation.seal()


def test_extend_rejects_nullable_nan_before_scanner_can_map_it_to_null(tmp_path):
    with _relation(tmp_path, batch_size=1) as relation:
        with pytest.raises(ValueError, match="g_star_z must be finite"):
            relation.extend((_event(0), replace(_event(1), g_star_z=float("nan"))))
        assert len(relation) == 1
        relation.seal()
        assert tuple(relation) == (_event(0),)


@pytest.mark.parametrize(
    "setup",
    [
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET ordinal=2")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET event_calculate_index=2")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET vehicle_id=NULL")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET lon='NaN'::DOUBLE")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET turn_deg=180")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET score=29")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET point_source_id='partial'")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "UPDATE turn_events SET refined_point_kind='kept'")),
        lambda r: (r.append(_event(0)), r._connection.execute(
            "INSERT INTO source_ids VALUES (99,0,'orphan')")),
        lambda r: (r.append(_event(0, source_ids=('one','two'))), r._connection.execute(
            "UPDATE source_ids SET source_ordinal=4 WHERE source_id='two'")),
        lambda r: (r.append(_event(0, source_ids=('one',))), r._connection.execute(
            "UPDATE source_ids SET source_id=''")),
    ],
)
def test_seal_rejects_corruption_and_closes(tmp_path, setup):
    relation = _relation(tmp_path)
    setup(relation)
    with pytest.raises(ValueError, match="invalid TurnEvent relation"):
        relation.seal()
    with pytest.raises(RuntimeError, match="closed"):
        len(relation)


def test_sealed_and_closed_lifecycle(tmp_path):
    relation = _relation(tmp_path)
    relation.append(_event(0))
    relation.seal()
    relation.seal()
    with pytest.raises(RuntimeError, match="sealed"):
        relation.append(_event(1))
    relation.close()
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation)
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_coordinates())


@pytest.mark.parametrize("batch_size", [True, 0])
def test_invalid_batch_size_is_rejected(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        TurnEventRelation(RuntimeConfig(), tmp_path, batch_size=batch_size)


@pytest.mark.parametrize("root", ["relative", "/"])
def test_unsafe_data_root_is_rejected(root):
    with pytest.raises(ValueError, match="absolute and non-root"):
        TurnEventRelation(RuntimeConfig(), root)


@pytest.mark.parametrize("relation_id", [".", "..", "bad/name", "bad\x00name"])
def test_unsafe_relation_identity_is_rejected(tmp_path, relation_id):
    with pytest.raises(ValueError, match="relation_id"):
        TurnEventRelation(RuntimeConfig(), tmp_path, relation_id=relation_id)


def test_connection_failure_removes_only_its_empty_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "turn-event-relations" / "keep"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(
        turn_event_relation.duckdb, "connect",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("open failed")),
    )
    with pytest.raises(RuntimeError, match="open failed"):
        TurnEventRelation(RuntimeConfig(), tmp_path, relation_id="failed")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_relation_identity_collision_fails_closed(tmp_path):
    occupied = tmp_path / "duckdb" / "turn-event-relations" / "occupied"
    occupied.mkdir(parents=True)
    marker = occupied / "owned-by-other-run"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        TurnEventRelation(RuntimeConfig(), tmp_path, relation_id="occupied")
    assert marker.read_text() == "keep"


def test_constructor_failure_after_connect_closes_and_cleans(tmp_path, monkeypatch):
    real_connect = turn_event_relation.duckdb.connect
    connection = real_connect(":memory:")
    monkeypatch.setattr(turn_event_relation.duckdb, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(TurnEventRelation, "_create_schema", lambda _self: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        TurnEventRelation(RuntimeConfig(), tmp_path, relation_id="failed")
    with pytest.raises(Exception):
        connection.execute("SELECT 1")
    assert not (tmp_path / "duckdb" / "turn-event-relations" / "failed").exists()


def test_runtime_limits_and_columnar_implementation(tmp_path):
    with _relation(tmp_path) as relation:
        settings = dict(relation._connection.execute(
            "SELECT name,value FROM duckdb_settings() WHERE name IN "
            "('threads','memory_limit','max_temp_directory_size','temp_directory',"
            "'autoload_known_extensions','autoinstall_known_extensions')"
        ).fetchall())
        assert settings["threads"] == "1"
        assert settings["memory_limit"] == "122.0 MiB"
        assert settings["max_temp_directory_size"] == "244.1 MiB"
        assert settings["autoload_known_extensions"] == "false"
        assert settings["autoinstall_known_extensions"] == "false"
        assert settings["temp_directory"] == str(relation._temp_directory)
    source = inspect.getsource(turn_event_relation).lower()
    assert "left join source_ids" in inspect.getsource(TurnEventRelation._iterate).lower()
    assert "fetchmany" in inspect.getsource(TurnEventRelation.iter_coordinates).lower()
    assert "execute(" not in inspect.getsource(TurnEventRelation._decode).lower()
    assert "executemany" in inspect.getsource(TurnEventRelation.append).lower()
    assert "register" in inspect.getsource(TurnEventRelation._append_batch).lower()
    for forbidden in ("sqlite", "pickle", "temporarydirectory", "disksequence"):
        assert forbidden not in source
