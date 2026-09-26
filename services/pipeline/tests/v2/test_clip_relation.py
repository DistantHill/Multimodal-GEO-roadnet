"""Normalized DuckDB Clip relation behavior and corruption guards."""
from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import inspect
from pathlib import Path

import pytest

from track.v2.clips import clip_input as clip_relation
from track.v2.clips.clip_input import ClipRelation
from track.v2.models import ClipEndpoint, ClipPoint, RuntimeConfig, TrackClip


def _clip(
    clip_id: str, start_id: int | None = 1, end_id: int | None = 2,
    *, points: tuple[ClipPoint, ...] = (), ordinal: int = 0,
) -> TrackClip:
    return TrackClip(
        clip_id=clip_id, vehicle_id=f"v-{clip_id}", trip_id="trip",
        clip_type="between_intersection" if start_id is not None else "full_track",
        start=ClipEndpoint(
            "exit" if start_id is not None else "boundary", 1.0, 2.0,
            start_id, 10 if start_id is not None else None,
        ),
        end=ClipEndpoint(
            "entry" if end_id is not None else "boundary", 3.0, 4.0,
            end_id, 20 if end_id is not None else None,
        ),
        points=points, distance_m=42.5, point_count=len(points) + 2,
        subtrack_index=3, clip_ordinal_in_track=ordinal,
    )


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
    points = (ClipPoint(7, 1.5, 2.5), ClipPoint(8, 2.5, 3.5))
    clips = (_clip("b", points=points), _clip("a", 2, 1, ordinal=1), _clip("edge", None, None))
    with _relation(tmp_path) as relation:
        for clip in clips:
            relation.append(clip)
        with pytest.raises(RuntimeError, match="not sealed"):
            list(relation)
        relation.seal()
        assert tuple(relation) == tuple(relation) == clips
        assert relation[0] == relation[-3] == clips[0]
        assert relation[-1] == clips[-1]
        assert relation[1:] == clips[1:]
        with pytest.raises(IndexError):
            _ = relation[3]
        assert relation.get_by_clip_id("b") == clips[0]
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
        lambda r: (r.append(_clip("bad-ordinal", points=(ClipPoint(1, 1.0, 2.0),))),
                   r._connection.execute("UPDATE clip_points SET point_ordinal=2")),
        lambda r: (r.append(_clip("bad-float")), r._connection.execute(
            "UPDATE clips SET start_lon='NaN'::DOUBLE")),
        lambda r: (r.append(_clip("orphan")), r._connection.execute(
            "INSERT INTO clip_points VALUES (99,0,1,1.0,2.0)")),
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


def test_q44_relation_uses_only_duckdb_columnar_storage():
    source = inspect.getsource(clip_relation).lower()
    assert "duckdb.connect" in source
    assert "left join clip_points" in inspect.getsource(ClipRelation._iterate).lower()
    assert "execute(" not in inspect.getsource(ClipRelation._decode).lower()
    append_source = inspect.getsource(ClipRelation.append).lower()
    assert "executemany" in append_source and "islice" in append_source
    for forbidden in ("sqlite", "pickle", "temporarydirectory", "memmap"):
        assert forbidden not in source
