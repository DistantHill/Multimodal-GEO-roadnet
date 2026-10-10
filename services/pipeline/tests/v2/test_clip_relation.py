"""Normalized DuckDB Clip relation behavior and corruption guards."""
from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import numpy as np
from pathlib import Path
from dataclasses import FrozenInstanceError

import pytest

from track.v2.clips import clip_input as clip_relation
from track.v2.clips.clip_input import ClipRelation
from track.v2.models import RuntimeConfig, TrackClip


def _clip(
    clip_id: str, start_id: int | None = 1, end_id: int | None = 2,
    *, points: tuple[tuple, ...] = (), ordinal: int = 0,
) -> TrackClip:
    lonlat = np.asarray([(1.,2.), *((p[1],p[2]) for p in points), (3.,4.)], dtype=np.float64)
    return TrackClip(
        clip_id=clip_id, vehicle_id=f"v-{clip_id}", trip_id="trip",
        clip_type="between_intersection" if start_id is not None else "full_track",
        start_kind="exit" if start_id is not None else "boundary",
        end_kind="entry" if end_id is not None else "boundary",
        start_intersection_id=start_id, end_intersection_id=end_id,
        start_crossing_id=10 if start_id is not None else None,
        end_crossing_id=20 if end_id is not None else None,
        xy=lonlat.copy(), lonlat=lonlat, resolved_point_ids=np.asarray([p[0] for p in points], dtype=np.int64),
        start_index=0, end_index=len(points)+1, interior_start=1, interior_end=len(points)+1,
        distance_m=42.5, point_count=len(points)+2, subtrack_index=3, clip_ordinal_in_track=ordinal,
    )


def _same(actual: TrackClip, expected: TrackClip) -> bool:
    assert actual.as_dict() == expected.as_dict()
    np.testing.assert_array_equal(actual.xy, expected.xy)
    np.testing.assert_array_equal(actual.lonlat, expected.lonlat)
    assert not actual.xy.flags.writeable
    return True


def _relation(tmp_path: Path, *, batch_size: int = 1) -> ClipRelation:
    return ClipRelation(
        runtime_config(
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
        assert relation.count_pair((1, 2)) == 0


def test_lossless_lookup_repeat_and_batch_one(tmp_path):
    points = ((7, 1.5, 2.5), (8, 2.5, 3.5))
    clips = (_clip("b", points=points), _clip("a", 2, 1, ordinal=1), _clip("edge", None, None))
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        with pytest.raises(RuntimeError, match="not sealed"):
            list(relation)
        relation.seal()
        assert all(_same(a, b) for a,b in zip(relation, clips, strict=True))
        assert all(_same(a, b) for a,b in zip(relation, clips, strict=True))
        assert _same(relation[0], clips[0]) and _same(relation[-3], clips[0])
        assert _same(relation[-1], clips[-1])
        assert all(_same(a,b) for a,b in zip(relation[1:], clips[1:], strict=True))
        with pytest.raises(IndexError):
            _ = relation[3]
        assert _same(relation.get_by_clip_id("b"), clips[0])
        with pytest.raises(KeyError):
            relation.get_by_clip_id("missing")


def test_pair_filter_direction_order_and_count(tmp_path):
    clips = (_clip("forward", 1, 2), _clip("reverse", 2, 1), _clip("other", 1, 3))
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        relation.seal()
        assert [clip.clip_id for clip in relation.iter_pair((2, 1))] == ["forward", "reverse"]
        assert [clip.clip_id for clip in relation.iter_pair((1, 2), reverse=False)] == ["forward"]
        assert [clip.clip_id for clip in relation.iter_pair((1, 2), reverse=True)] == ["reverse"]
        assert relation.count_pair((2, 1)) == 2
        assert relation.count_pair((1, 2), reverse=False) == 1
        assert list(relation.iter_pair((1, 1))) == []


@pytest.mark.parametrize(
    "setup",
    [
        lambda r: (r.append(_clip("duplicate")), r.append(_clip("duplicate"))),
        lambda r: (r.append(_clip("bad-count")), r._connection.execute(
            "UPDATE clips SET point_count=99")),
        lambda r: (r.append(_clip("bad-ordinal", points=((1, 1.0, 2.0),))),
                   r._connection.execute("UPDATE clip_points SET point_ordinal=2")),
        lambda r: (r.append(_clip("bad-float")), r._connection.execute(
            "UPDATE clips SET start_lon='NaN'::DOUBLE")),
        lambda r: (r.append(_clip("orphan")), r._connection.execute(
            "INSERT INTO clip_points VALUES (99,0,1,1.0,2.0,1.0,2.0)")),
        lambda r: (r.append(_clip("bad-endpoint")), r._connection.execute(
            "UPDATE clips SET start_crossing_id=NULL")),
    ],
)
def test_seal_rejects_corruption_and_closes(tmp_path, setup):
    relation = _relation(tmp_path)
    setup(relation)
    with pytest.raises(ValueError, match="invalid Clip relation"):
        relation.seal()
    with pytest.raises(RuntimeError, match="closed"):
        len(relation)


def test_sealed_and_closed_lifecycle(tmp_path):
    relation = _relation(tmp_path)
    relation.append(_clip("one"))
    relation.seal()
    relation.seal()
    with pytest.raises(RuntimeError, match="sealed"):
        relation.append(_clip("two"))
    relation.close()
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation)
    with pytest.raises(RuntimeError, match="closed"):
        relation.get_by_clip_id("one")


@pytest.mark.parametrize("batch_size", [True, 0])
def test_invalid_batch_size_is_rejected(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        ClipRelation(runtime_config(), tmp_path, batch_size=batch_size)


@pytest.mark.parametrize("relation_id", [".", "..", "bad/name", "bad\x00name"])
def test_unsafe_relation_identity_is_rejected(tmp_path, relation_id):
    with pytest.raises(ValueError, match="relation_id"):
        ClipRelation(runtime_config(), tmp_path, relation_id=relation_id)


def test_relative_data_root_is_rejected():
    with pytest.raises(ValueError, match="absolute"):
        ClipRelation(runtime_config(), "relative")


def test_connection_failure_removes_only_its_empty_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "clip-relations" / "keep"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(
        clip_relation.duckdb, "connect",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("open failed")),
    )
    with pytest.raises(RuntimeError, match="open failed"):
        ClipRelation(runtime_config(), tmp_path, relation_id="failed")
    assert sibling.is_dir()
    assert not (sibling.parent / "failed").exists()


def test_relation_identity_collision_fails_closed(tmp_path):
    occupied = tmp_path / "duckdb" / "clip-relations" / "occupied"
    occupied.mkdir(parents=True)
    marker = occupied / "owned-by-other-run"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        ClipRelation(runtime_config(), tmp_path, relation_id="occupied")
    assert marker.read_text() == "keep"


def test_runtime_limits_and_engine_temp_are_applied(tmp_path):
    with _relation(tmp_path) as relation:
        settings = dict(relation._connection.execute(
            "SELECT name,value FROM duckdb_settings() WHERE name IN "
            "('threads','memory_limit','max_temp_directory_size','temp_directory',"
            "'autoload_known_extensions','autoinstall_known_extensions')"
        ).fetchall())
        assert settings["threads"] == "1"
        assert settings["memory_limit"] == "122.0 MiB"
        assert settings["max_temp_directory_size"] == "244.1 MiB"
        assert settings["temp_directory"] == str(
            tmp_path / "duckdb" / "clip-relations" / "test-relation"
        )
        assert settings["autoload_known_extensions"] == "false"
        assert settings["autoinstall_known_extensions"] == "false"
        assert not list((tmp_path / "duckdb" / "clip-relations").glob("*.duckdb"))


def test_batch_append_reads_across_clips_and_metadata(tmp_path):
    clips = (
        _clip("a", points=((2**53 + 1, 1.0, 2.0),), ordinal=0),
        _clip("b", points=((2**53 + 2, 1.5, 2.5),), ordinal=1),
        _clip("c", ordinal=2),
    )
    with _relation(tmp_path, batch_size=3) as relation:
        relation.append_batch(clips)
        relation.seal()
        assert len(relation) == 3
        assert all(_same(actual, expected) for actual, expected in zip(relation, clips, strict=True))
        assert all(_same(actual, expected) for actual, expected in zip(relation, clips, strict=True))
        assert relation.get_clip_metadata_batch(["b", "a", "b"]) == [
            ("between_intersection", 10, 20, 1),
            ("between_intersection", 10, 20, 1),
            ("between_intersection", 10, 20, 1),
        ]
        with pytest.raises(KeyError, match="missing"):
            relation.get_clip_metadata_batch(["a", "missing"])


def test_batch_append_rolls_back_failed_point_insert(tmp_path, monkeypatch):
    with _relation(tmp_path, batch_size=2) as relation:
        original = relation._column_insert
        monkeypatch.setattr(
            relation, "_column_insert",
            lambda table, types: "INSERT INTO nonexistent SELECT 1" if table == "clip_points"
            else original(table, types),
        )
        with pytest.raises(Exception):
            relation.append_batch((_clip("a", points=((1, 1.0, 2.0),)),
                                   _clip("b", points=((2, 1.5, 2.5),), ordinal=1)))
        assert len(relation) == 0
        assert relation._connection.execute("SELECT count(*) FROM clips").fetchone()[0] == 0
        monkeypatch.setattr(relation, "_column_insert", original)
        relation.append(_clip("retry"))
        relation.seal()
        assert [clip.clip_id for clip in relation] == ["retry"]


def test_wide_json_columns_preserve_large_int64_and_xy(tmp_path):
    from dataclasses import replace
    size = 4096
    xy = np.column_stack((np.linspace(1.0, 3.0, size+2), np.linspace(2.0, 4.0, size+2)))
    xy[1] = [-0.0, -0.0]
    clip = replace(_clip("wide"), xy=xy.copy(), lonlat=xy.copy(),
                   resolved_point_ids=np.arange(size,dtype=np.int64)+2**53,
                   end_index=size+1, interior_end=size+1, point_count=size+2)
    with _relation(tmp_path, batch_size=1024) as relation:
        relation.append(clip)
        relation.seal()
        result = next(iter(relation))
        np.testing.assert_array_equal(result.resolved_point_ids, clip.resolved_point_ids)
        np.testing.assert_array_equal(result.xy, clip.xy)
        np.testing.assert_array_equal(result.lonlat, clip.lonlat)
        assert np.signbit(result.xy[1]).all()


def test_fitting_metadata_is_scalar_ordered_immutable_and_never_reads_points(tmp_path, monkeypatch):
    clips = (_clip("z", points=((7, 1.5, 2.5),)), _clip("a", 2, 1),
             _clip("boundary", None, None), _clip("same", 1, 1))
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        with pytest.raises(RuntimeError, match="not sealed"):
            list(relation.iter_fitting_metadata())
        relation.seal()

        def no_points(*args):
            pytest.fail("fitting metadata decoded point geometry")

        monkeypatch.setattr(relation, "_read_point_batch", no_points)
        values = list(relation.iter_fitting_metadata())
        assert [(v.ordinal, v.clip_id, v.clip_type, v.start_intersection_id,
                 v.end_intersection_id, v.point_count) for v in values] == [
            (i, c.clip_id, c.clip_type, c.start_intersection_id,
             c.end_intersection_id, c.point_count) for i, c in enumerate(clips)
        ]
        assert not hasattr(values[0], "__dict__")
        with pytest.raises(FrozenInstanceError):
            values[0].ordinal = 99
        stream = relation.iter_fitting_metadata()
        assert next(stream) == values[0]
        stream.close()
        assert list(relation.iter_fitting_metadata()) == values
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_fitting_metadata())


def test_fitting_pair_filters_type_and_endpoints_and_keeps_original_ordinal(tmp_path):
    clips = (_clip("z", 1, 2, points=((7, 1.5, 2.5),)), _clip("a", 2, 1),
             _clip("boundary", None, None), _clip("same", 1, 1), _clip("other", 1, 3))
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        relation.seal()
        for pair in ((1, 2), (2, 1)):
            found = list(relation.iter_fitting_pair(pair))
            assert [ordinal for ordinal, _ in found] == [1, 0]
            assert all(_same(actual, clips[ordinal]) for ordinal, actual in found)
            assert [i for i, _ in relation.iter_fitting_pair(pair, reverse=False)] == [0]
            assert [i for i, _ in relation.iter_fitting_pair(pair, reverse=True)] == [1]
        assert list(relation.iter_fitting_pair((1, 1))) == []
        stream = relation.iter_fitting_pair((1, 2))
        assert next(stream)[0] == 1
        stream.close()
        assert [i for i, _ in relation.iter_fitting_pair((1, 2))] == [1, 0]


def test_fitting_readers_close_owned_cursors_on_partial_read_and_decode_failure(tmp_path, monkeypatch):
    class CursorObserver:
        def __init__(self, value):
            self.value = value
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.value, name)

        def close(self):
            self.closed = True
            self.value.close()

    class ConnectionObserver:
        def __init__(self, value):
            self.value = value
            self.cursors = []

        def __getattr__(self, name):
            return getattr(self.value, name)

        def cursor(self):
            cursor = CursorObserver(self.value.cursor())
            self.cursors.append(cursor)
            return cursor

    with _relation(tmp_path) as relation:
        relation.append(_clip("a", points=((7, 1.5, 2.5),)))
        relation.append(_clip("b"))
        relation.seal()
        observer = ConnectionObserver(relation._connection)
        monkeypatch.setattr(relation, "_connection", observer)
        for method in (relation.iter_fitting_metadata, lambda: relation.iter_fitting_pair((1, 2))):
            stream = method()
            next(stream)
            stream.close()
            assert observer.cursors
            assert all(cursor.closed for cursor in observer.cursors)
        original = relation._decode

        def fail(*args):
            raise RuntimeError("decode failed")

        monkeypatch.setattr(relation, "_decode", fail)
        with pytest.raises(RuntimeError, match="decode failed"):
            list(relation.iter_fitting_pair((1, 2)))
        assert all(cursor.closed for cursor in observer.cursors)
        monkeypatch.setattr(relation, "_decode", original)
        assert [i for i, _ in relation.iter_fitting_pair((1, 2))] == [0, 1]
        assert len(relation) == 2
