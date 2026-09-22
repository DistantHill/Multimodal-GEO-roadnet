"""Normalized DuckDB cross-candidate relation behavior and guards."""
from __future__ import annotations

from collections.abc import Sequence
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from track.v2 import cross_candidate_relation
from track.v2.cross_candidate_relation import (
    ClusterMemberSequence, CrossCandidateCoordinate, CrossCandidateRelation,
)
from track.v2.member_sequences import SliceSequence
from track.v2.models import CrossTrackCandidate, PointRef, RefinedPointRef, RuntimeConfig


def _candidate(index: int, *, optional: bool = False) -> CrossTrackCandidate:
    point = PointRef("source", f"row-{index}", True, "from", None, "", "hash")
    other = PointRef("other", f"other-{index}", False, None, "to", "derived", "hash-2")
    return CrossTrackCandidate(
        point_ref=point, from_segment_start_point_ref=point,
        to_segment_start_point_ref=other, vehicle_id="a|b", timestamp=10.5 + index,
        lon=120.0, lat=30.0, x=1.0 + index, y=2.0 + index,
        heading=359.5, speed=3.25,
        from_segment_start=RefinedPointRef("kept", 4, 2, 8) if optional else None,
        to_segment_start=RefinedPointRef("injected", 5, 3, 9) if optional else None,
        cross_track_calculate_index=index,
    )


def _relation(tmp_path: Path, *, batch_size: int = 1) -> CrossCandidateRelation:
    return CrossCandidateRelation(
        RuntimeConfig(
            duckdb_memory_limit="128MB", duckdb_threads=1,
            duckdb_max_temp_directory_size="256MB",
        ),
        tmp_path, batch_size=batch_size, relation_id="test-relation",
    )


def test_empty_relation_seals_and_repeats(tmp_path):
    with _relation(tmp_path) as relation:
        relation.seal()
        assert len(relation) == 0
        assert list(relation) == list(relation) == []
        assert list(relation.iter_coordinates()) == []


def test_lossless_refs_index_slices_coordinates_and_batch_one(tmp_path):
    candidates = (_candidate(0, optional=True), _candidate(1))
    with _relation(tmp_path) as relation:
        for candidate in candidates:
            relation.append(candidate)
        with pytest.raises(RuntimeError, match="not sealed"):
            list(relation)
        relation.seal()
        assert tuple(relation) == tuple(relation) == candidates
        assert relation[0] == relation[-2] == candidates[0]
        assert relation[-1] == candidates[1]
        assert relation[:] == candidates
        assert relation[::2] == candidates[::2]
        assert isinstance(relation[::-1], SliceSequence)
        assert relation[::-1] == candidates[::-1]
        assert relation == list(candidates)
        assert candidates[0].as_dict() == relation[0].as_dict()
        assert list(relation.iter_coordinates()) == [
            CrossCandidateCoordinate(0, 1.0, 2.0),
            CrossCandidateCoordinate(1, 2.0, 3.0),
        ]
        with pytest.raises(IndexError):
            _ = relation[2]
        with pytest.raises(TypeError):
            _ = relation[1.5]  # type: ignore[index]


@pytest.mark.parametrize("batch_size", [1, 2, 5])
def test_extend_is_lossless_and_preserves_nullable_bigints(tmp_path, batch_size):
    huge = 2**60 + 9
    candidates = (
        _candidate(0),
        replace(
            _candidate(1, optional=True),
            from_segment_start=RefinedPointRef("kept", huge, huge + 1, huge + 2),
            to_segment_start=RefinedPointRef("injected", huge + 3, huge + 4, huge + 5),
        ),
        _candidate(2),
    )
    with _relation(tmp_path, batch_size=2) as relation:
        relation.extend(iter(candidates), batch_size=batch_size)
        relation.seal()
        assert tuple(relation) == candidates


def test_extend_insert_failure_rolls_back_batch_and_unregisters(tmp_path):
    first = replace(_candidate(0), vehicle_id="first")
    with _relation(tmp_path, batch_size=2) as relation:
        relation.extend((first,), batch_size=1)
        relation._connection.execute(
            "CREATE UNIQUE INDEX cross_vehicle_unique ON cross_candidates(vehicle_id)"
        )
        with pytest.raises(Exception, match="unique|duplicate|constraint"):
            relation.extend((replace(_candidate(1), vehicle_id="duplicate"),
                             replace(_candidate(2), vehicle_id="duplicate")))
        assert len(relation) == 1
        assert relation._connection.execute(
            "SELECT count(*) FROM cross_candidates"
        ).fetchone() == (1,)
        assert relation._connection.execute(
            "SELECT count(*) FROM duckdb_views() WHERE view_name='cross_candidate_batch'"
        ).fetchone() == (0,)
        second = replace(_candidate(1), vehicle_id="second")
        relation.extend((second,))
        relation.seal()
        assert tuple(relation) == (first, second)


def test_extend_nan_cannot_bypass_relation_validation(tmp_path):
    relation = _relation(tmp_path)
    relation.extend((replace(_candidate(0), x=float("nan")),))
    with pytest.raises(ValueError, match="invalid CrossTrackCandidate relation"):
        relation.seal()


@pytest.mark.parametrize("field", ["source_id", "source_row_key",
                                    "generated_reason", "source_row_hash"])
def test_extend_and_append_reject_the_same_null_point_strings(tmp_path, field):
    invalid_point = replace(_candidate(0).point_ref, **{field: None})
    invalid = replace(
        _candidate(0), point_ref=invalid_point,
        from_segment_start_point_ref=invalid_point,
    )
    for method in ("append", "extend"):
        relation = _relation(tmp_path / method)
        getattr(relation, method)((invalid,) if method == "extend" else invalid)
        with pytest.raises(ValueError, match="invalid CrossTrackCandidate relation"):
            relation.seal()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE cross_candidates SET ordinal=2",
        "UPDATE cross_candidates SET cross_track_calculate_index=2",
        "UPDATE cross_candidates SET vehicle_id=NULL",
        "UPDATE cross_candidates SET x='NaN'::DOUBLE",
        "UPDATE cross_candidates SET lon=181",
        "UPDATE cross_candidates SET point_source_id=''",
        "UPDATE cross_candidates SET from_point_stable=NULL",
        "UPDATE cross_candidates SET from_id=NULL",
        "UPDATE cross_candidates SET to_id=-1,to_kind='kept',to_subtrack=0,to_sequence=0",
    ],
)
def test_seal_rejects_corruption_and_closes(tmp_path, sql):
    relation = _relation(tmp_path)
    relation.append(_candidate(0, optional=True))
    relation._connection.execute(sql)
    with pytest.raises(ValueError, match="invalid CrossTrackCandidate relation"):
        relation.seal()
    with pytest.raises(RuntimeError, match="closed"):
        len(relation)


def test_sealed_and_closed_lifecycle(tmp_path):
    relation = _relation(tmp_path)
    relation.append(_candidate(0))
    relation.seal()
    relation.seal()
    with pytest.raises(RuntimeError, match="sealed"):
        relation.append(_candidate(1))
    relation.close()
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation)
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_coordinates())


def test_seal_rejects_non_contiguous_appended_calculate_index(tmp_path):
    relation = _relation(tmp_path)
    relation.append(replace(_candidate(0), cross_track_calculate_index=4))
    with pytest.raises(ValueError, match="invalid CrossTrackCandidate relation"):
        relation.seal()
    with pytest.raises(RuntimeError, match="closed"):
        len(relation)


@pytest.mark.parametrize("batch_size", [True, 0])
def test_invalid_batch_size_is_rejected(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        CrossCandidateRelation(RuntimeConfig(), tmp_path, batch_size=batch_size)


@pytest.mark.parametrize("root", ["relative", "/"])
def test_unsafe_data_root_is_rejected(root):
    with pytest.raises(ValueError, match="absolute and non-root"):
        CrossCandidateRelation(RuntimeConfig(), root)


@pytest.mark.parametrize("relation_id", [".", "..", "bad/name", "bad\x00name"])
def test_unsafe_relation_identity_is_rejected(tmp_path, relation_id):
    with pytest.raises(ValueError, match="relation_id"):
        CrossCandidateRelation(RuntimeConfig(), tmp_path, relation_id=relation_id)


def test_connection_failure_removes_only_its_empty_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "cross-candidate-relations" / "keep"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(
        cross_candidate_relation.duckdb, "connect",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("open failed")),
    )
    with pytest.raises(RuntimeError, match="open failed"):
        CrossCandidateRelation(RuntimeConfig(), tmp_path, relation_id="failed")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_relation_identity_collision_fails_closed(tmp_path):
    occupied = tmp_path / "duckdb" / "cross-candidate-relations" / "occupied"
    occupied.mkdir(parents=True)
    marker = occupied / "owned-by-other-run"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        CrossCandidateRelation(RuntimeConfig(), tmp_path, relation_id="occupied")
    assert marker.read_text() == "keep"


def test_constructor_failure_after_connect_closes_and_cleans(tmp_path, monkeypatch):
    connection = cross_candidate_relation.duckdb.connect(":memory:")
    monkeypatch.setattr(cross_candidate_relation.duckdb, "connect", lambda **_kwargs: connection)
    monkeypatch.setattr(CrossCandidateRelation, "_create_schema", lambda _self: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        CrossCandidateRelation(RuntimeConfig(), tmp_path, relation_id="failed")
    with pytest.raises(Exception):
        connection.execute("SELECT 1")
    assert not (tmp_path / "duckdb" / "cross-candidate-relations" / "failed").exists()


def test_runtime_limits_bounded_fetch_and_columnar_implementation(tmp_path):
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
    source = inspect.getsource(cross_candidate_relation).lower()
    assert "fetchmany" in inspect.getsource(CrossCandidateRelation._iterate).lower()
    assert "fetchmany" in inspect.getsource(CrossCandidateRelation.iter_coordinates).lower()
    assert "execute(" not in inspect.getsource(CrossCandidateRelation._decode).lower()
    assert "register" in inspect.getsource(CrossCandidateRelation._append_batch).lower()
    for forbidden in ("sqlite", "pickle", "struct", "temporarydirectory"):
        assert forbidden not in source


def test_member_pass_has_independent_expected_views_and_ignores_noise(tmp_path):
    candidates = tuple(_candidate(index, optional=index % 2 == 0) for index in range(5))
    labels = [4, -1, 2, 4, 2]
    with _relation(tmp_path) as relation:
        for candidate in candidates:
            relation.append(candidate)
        relation_pass = relation.create_members(labels)
        actual = dict(relation_pass.iter_groups())
        expected_ordinals = {2: [2, 4], 4: [0, 3]}
        assert list(actual) == list(expected_ordinals) == [2, 4]
        for label, ordinals in expected_ordinals.items():
            group = actual[label]
            expected_candidates = [candidates[index] for index in ordinals]
            assert isinstance(group, ClusterMemberSequence)
            assert [event.cross_track_calculate_index for event in group] == ordinals
            assert group[:] == tuple(group)
            assert isinstance(group[::-1], SliceSequence)
            assert group[::-1] == list(group)[::-1]
            assert group == list(group)
            assert group[-1].cross_track_calculate_index == ordinals[-1]
            assert list(group.iter_member_identities()) == [
                ("orthogonal_cross_track", order, ordinal)
                for order, ordinal in enumerate(ordinals)
            ]
            assert list(group.iter_ordinals()) == ordinals
            assert list(group.iter_coordinates()) == [
                (candidate.x, candidate.y) for candidate in expected_candidates
            ]
            assert list(group.iter_candidates()) == expected_candidates
            summary = group.provenance_summary("ignored")
            assert summary.event_source_ids == frozenset({"source", "other"})
            assert summary.ref_counts == {"orthogonal_cross_track": len(ordinals)}
            assert summary.ref_source_ids == {
                "orthogonal_cross_track": frozenset({"source"})
            }
            assert tuple(group) == tuple(group)


def test_multiple_member_passes_close_independently(tmp_path):
    with _relation(tmp_path) as relation:
        for index in range(3):
            relation.append(_candidate(index))
        first = relation.create_members([0, 0, -1])
        second = relation.create_members([1, -1, 1])
        first_group = dict(first.iter_groups())[0]
        second_group = dict(second.iter_groups())[1]
        iterator = iter(first_group)
        assert next(iterator).cross_track_calculate_index == 0
        iterator.close()
        first.close()
        with pytest.raises(RuntimeError, match="member pass is closed"):
            list(first_group)
        assert list(second_group.iter_ordinals()) == [0, 2]
        second.close()


def test_member_label_length_and_failed_conversion_roll_back(tmp_path):
    with _relation(tmp_path) as relation:
        for index in range(3):
            relation.append(_candidate(index))
        with pytest.raises(ValueError, match="one DBSCAN label"):
            relation.create_members([0, 1])
        with pytest.raises(ValueError):
            relation.create_members([0, "bad", 1])  # type: ignore[list-item]
        assert relation._connection.execute(
            "SELECT count(*) FROM cross_candidate_members"
        ).fetchone()[0] == 0
        valid = relation.create_members([-1, 7, 7])
        assert list(dict(valid.iter_groups())[7].iter_ordinals()) == [1, 2]


@pytest.mark.parametrize("items", [[0, 1], [0, 1, 2, 3]])
def test_member_iteration_length_mismatch_rolls_back(tmp_path, items):
    class LyingLabels(Sequence):
        def __len__(self):
            return 3

        def __getitem__(self, index):
            return items[index]

    with _relation(tmp_path) as relation:
        for index in range(3):
            relation.append(_candidate(index))
        with pytest.raises(ValueError, match="one DBSCAN label"):
            relation.create_members(LyingLabels())
        assert relation._connection.execute(
            "SELECT count(*) FROM cross_candidate_members"
        ).fetchone()[0] == 0


def test_relation_close_invalidates_member_views(tmp_path):
    relation = _relation(tmp_path)
    relation.append(_candidate(0))
    member_pass = relation.create_members([0])
    group = dict(member_pass.iter_groups())[0]
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(group)
    member_pass.close()
