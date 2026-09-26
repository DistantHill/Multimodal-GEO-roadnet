"""First-phase DuckDB writer relation behavior and lifecycle."""
from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import inspect
import math
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from track.v2.intersections.relations import source_associations as writer_relation
from track.v2.models import RefinedPointRef, RuntimeConfig
from track.v2.intersections.relations.source_associations import WriterRelation
from track.v2.db import write_detect_results as db_writer


def _relation(tmp_path: Path, *, batch_size: int = 2) -> WriterRelation:
    return WriterRelation(
        runtime_config(duckdb_memory_limit="128MB", duckdb_threads=1,
                      duckdb_max_temp_directory_size="256MB"),
        tmp_path, "detect", batch_size=batch_size, relation_id="relation",
    )


@pytest.mark.parametrize("failure", [None, RuntimeError("failed"), KeyboardInterrupt()])
def test_persist_detect_result_owns_relation_lifecycle(tmp_path, monkeypatch, failure):
    runtime = runtime_config()
    events = []

    class Relation:
        def __init__(self, actual_runtime, data_root, detect_id, *, batch_size):
            events.append((actual_runtime, data_root, detect_id, batch_size))

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_args):
            events.append("close")

    def persist(*_args, **_kwargs):
        if failure is not None:
            raise failure
        return "written"

    monkeypatch.setattr(db_writer, "WriterRelation", Relation)
    monkeypatch.setattr(db_writer, "_persist_detect_result", persist)
    if failure is None:
        assert db_writer.persist_detect_result(
            object(), "detect", object(), runtime=runtime, data_root=tmp_path, batch_size=7,
        ) == "written"
    else:
        with pytest.raises(type(failure)):
            db_writer.persist_detect_result(
                object(), "detect", object(), runtime=runtime,
                data_root=tmp_path, batch_size=7,
            )
    assert events == [(runtime, tmp_path, "detect", 7), "enter", "close"]


def _turn(index, ref):
    return NS(event_calculate_index=index, refined_point_ref=ref,
              entry_bearing=10.0 + index, exit_bearing=20.0,
              turn_deg=-10.0, score=0.5)


def _cross(index, first, second):
    return NS(cross_track_calculate_index=index, from_segment_start=first,
              to_segment_start=second, x=index + 0.1, y=2.0, lon=113.0, lat=23.0)


def _crossing(ref):
    return NS(segment_start=ref, is_entry=True, crossing_x=1.0, crossing_y=2.0,
              crossing_lon=113.0, crossing_lat=23.0)


def _member(source, event_index, cross_index=None):
    return NS(source=source, event_calculate_index=event_index,
              cross_track_calculate_index=cross_index)


def _resolve_for_annotations(relation, refs, turn_indices, cross_indices):
    relation.collect_sources(
        NS(turn_events=[_turn(index, refs[0]) for index in turn_indices],
           cross_track_candidates=[_cross(index, refs[0], refs[1])
                                   for index in cross_indices]),
        [], [], {},
    )
    relation.add_mapping(refs[0], 100)
    relation.add_mapping(refs[1], 101)
    relation.resolve_sources()


class ClosingIterable:
    def __init__(self, values, failure=None):
        self.values, self.failure, self.closed = values, failure, False

    def __iter__(self):
        try:
            yield from self.values
            if self.failure is not None:
                raise self.failure
        finally:
            self.closed = True


def test_collect_resolve_exact_keys_last_wins_and_order_across_batches(tmp_path):
    refs = [RefinedPointRef("kept", 7, 2, 0),
            RefinedPointRef("injected", 7, 2, 1),
            RefinedPointRef("kept", 7, 3, 0)]
    turns = ClosingIterable([_turn(9, refs[2]), _turn(4, refs[0])])
    crosses = ClosingIterable([_cross(8, refs[1], refs[0]),
                               _cross(3, refs[2], refs[1])])
    pending = ClosingIterable([(NS(cluster_calculate_index=5), _crossing(refs[0]))])
    crossing_ids = ClosingIterable([900])
    with _relation(tmp_path, batch_size=1) as relation:
        relation.collect_sources(NS(turn_events=turns, cross_track_candidates=crosses),
                                 pending, crossing_ids, {5: 50})
        assert relation.counts == [2, 2, 1]
        assert list(relation.iter_raw("turn")) == [
            (0, 19.0, 20.0, -10.0, 0.5), (1, 14.0, 20.0, -10.0, 0.5)]
        assert [row[0] for row in relation.iter_raw("cross")] == [0, 1]
        assert list(relation.iter_raw("crossing")) == [
            (0, 900, 50, 1, 1.0, 2.0, 113.0, 23.0)]
        assert list(relation.iter_annotation_requests()) == [
            ("turn", 9, 0), ("turn", 4, 1), ("cross", 8, 0), ("cross", 3, 1)]
        for resolved_id, ref in enumerate(refs, 100):
            relation.add_mapping(ref, resolved_id)
        relation.add_mapping(refs[0], 999)
        relation.resolve_sources()
        assert list(relation.iter_resolved_sources(0)) == [(0, 0, 102), (1, 0, 999)]
        assert list(relation.iter_resolved_sources(1)) == [
            (0, 0, 101), (0, 1, 999), (1, 0, 102), (1, 1, 101)]
        assert list(relation.iter_resolved_sources(2)) == [(0, 0, 999)]
        relation.resolve_sources()
        with pytest.raises(RuntimeError, match="resolved"):
            relation.add_mapping(refs[0], 1)
        with pytest.raises(RuntimeError, match="resolved"):
            relation.add_request(0, 2, 0, refs[0])
    assert turns.closed and crosses.closed and pending.closed and crossing_ids.closed


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_add_mappings_bounded_typed_order_and_large_ints(tmp_path, count):
    refs = [
        RefinedPointRef("kept", 2**53 + index, 10 + index, 0)
        for index in range(count)
    ]
    with _relation(tmp_path, batch_size=2) as relation:
        relation.add_mappings((ref, 2**53 + 100 + index)
                              for index, ref in enumerate(refs))
        rows = relation._connection.execute("""
          SELECT mapping_order,point_kind,point_id,subtrack_index,resolved_id
          FROM source_mappings ORDER BY mapping_order
        """).fetchall()
        assert rows == [
            (index, "kept", ref.point_id, ref.subtrack_index, 2**53 + 100 + index)
            for index, ref in enumerate(refs)
        ]


def test_add_mappings_preserves_last_mapping_wins(tmp_path):
    ref = RefinedPointRef("kept", 7, 2, 0)
    with _relation(tmp_path, batch_size=2) as relation:
        relation.add_request(0, 0, 0, ref)
        relation.add_mappings([(ref, 100), (ref, 999)])
        relation.resolve_sources()
        assert list(relation.iter_resolved_sources(0)) == [(0, 0, 999)]


def test_add_mappings_sql_failure_rolls_back_batch_and_unregisters(tmp_path):
    refs = [RefinedPointRef("kept", index, 1, index) for index in range(3)]
    mappings = ClosingIterable([(ref, 100 + index) for index, ref in enumerate(refs)])
    with _relation(tmp_path, batch_size=2) as relation:
        relation._connection.execute(
            "INSERT INTO source_mappings VALUES (2,'kept',999,9,999)"
        )
        with pytest.raises(Exception, match="duplicate key|constraint"):
            relation.add_mappings(mappings)
        assert relation._mapping_order == 2
        assert relation._connection.execute("""
          SELECT mapping_order,point_id,resolved_id FROM source_mappings
          ORDER BY mapping_order
        """).fetchall() == [(0, 0, 100), (1, 1, 101), (2, 999, 999)]
        assert relation._connection.execute("""
          SELECT count(*) FROM duckdb_views()
          WHERE view_name LIKE 'source_mapping_batch_%'
        """).fetchone() == (0,)
    assert mappings.closed


@pytest.mark.parametrize("field,value", [
    ("point_id", 1.5), ("subtrack_index", True), ("resolved_id", "7"),
    ("point_id", 2**63),
])
def test_add_mappings_rejects_lossy_typed_values(tmp_path, field, value):
    values = {"point_id": 1, "subtrack_index": 2, "resolved_id": 3}
    values[field] = value
    ref = NS(point_kind="kept", point_id=values["point_id"],
             subtrack_index=values["subtrack_index"])
    with _relation(tmp_path) as relation:
        with pytest.raises(TypeError, match="invalid types"):
            relation.add_mappings([(ref, values["resolved_id"])])
        assert relation._mapping_order == 0
        assert relation._connection.execute(
            "SELECT count(*) FROM source_mappings"
        ).fetchone() == (0,)


def test_missing_key_reports_exact_three_field_identity(tmp_path):
    ref = RefinedPointRef("kept", 100, 4, 9)
    with _relation(tmp_path) as relation:
        relation.add_request(0, 0, 0, ref)
        relation.add_mapping(RefinedPointRef("kept", 100, 5, 9), 1)
        relation.add_mapping(RefinedPointRef("injected", 100, 4, 9), 2)
        with pytest.raises(KeyError) as error:
            relation.resolve_sources()
        assert error.value.args == (("kept", 100, 4),)


@pytest.mark.parametrize("stage,failure", [
    ("success", None), ("turn", RuntimeError("broken")),
    ("cross", KeyboardInterrupt()), ("crossing", RuntimeError("broken")),
    ("crossing_ids", KeyboardInterrupt()),
])
def test_input_iterators_close_on_success_and_base_exception(tmp_path, stage, failure):
    ref = RefinedPointRef("kept", 1, 1, 0)
    turns = ClosingIterable([_turn(0, ref)], failure if stage == "turn" else None)
    crosses = ClosingIterable([_cross(0, ref, ref)] if stage == "cross" else [],
                              failure if stage == "cross" else None)
    crossing_rows = [(NS(cluster_calculate_index=1), _crossing(ref))]
    if stage == "crossing_ids":
        crossing_rows *= 2
    pending = ClosingIterable(
        crossing_rows if stage.startswith("crossing") else [],
        failure if stage == "crossing" else None,
    )
    crossing_ids = ClosingIterable([4, 5] if stage.startswith("crossing") else [],
                                   failure if stage == "crossing_ids" else None)
    relation = _relation(tmp_path)
    try:
        if failure is None:
            relation.collect_sources(NS(turn_events=turns, cross_track_candidates=crosses),
                                     pending, crossing_ids, {1: 2})
        else:
            with pytest.raises(type(failure)):
                relation.collect_sources(NS(turn_events=turns, cross_track_candidates=crosses),
                                         pending, crossing_ids, {1: 2})
        assert turns.closed
        if stage != "turn":
            assert crosses.closed
        if stage.startswith("crossing"):
            assert pending.closed and crossing_ids.closed
    finally:
        relation.close()


def test_collect_sources_preserves_nullable_nan_and_infinite_raw_values(tmp_path):
    ref = RefinedPointRef("kept", 2**53 + 1, 2**53 + 2, 0)
    turn = NS(event_calculate_index=2**53 + 3, refined_point_ref=ref,
              entry_bearing=None, exit_bearing=float("nan"),
              turn_deg=float("inf"), score=float("-inf"))
    crossing = NS(segment_start=ref, is_entry=False, crossing_x=None,
                  crossing_y=float("nan"), crossing_lon=float("inf"),
                  crossing_lat=float("-inf"))
    with _relation(tmp_path, batch_size=2) as relation:
        relation.collect_sources(
            NS(turn_events=[turn], cross_track_candidates=[]),
            [(NS(cluster_calculate_index=9), crossing)], [None], {9: None},
        )
        turn_row = list(relation.iter_raw("turn"))[0]
        assert turn_row[1] is None and math.isnan(turn_row[2])
        assert turn_row[3:] == (float("inf"), float("-inf"),)
        crossing_row = list(relation.iter_raw("crossing"))[0]
        assert crossing_row[:4] == (0, None, None, 0)
        assert crossing_row[4] is None and math.isnan(crossing_row[5])
        assert crossing_row[6:] == (float("inf"), float("-inf"),)
        requests = relation._connection.execute("""
          SELECT point_id,subtrack_index FROM source_requests ORDER BY request_order
        """).fetchall()
        assert requests == [(2**53 + 1, 2**53 + 2)] * 2


def test_collect_sources_failed_batch_is_atomic_and_retryable(tmp_path):
    ref = RefinedPointRef("kept", 1, 1, 0)
    turns = ClosingIterable([_turn(index, ref) for index in range(3)])
    with _relation(tmp_path, batch_size=2) as relation:
        relation._connection.execute("INSERT INTO turn_raw VALUES (2,0,0,0,0)")
        with pytest.raises(Exception, match="duplicate key|constraint"):
            relation.collect_sources(
                NS(turn_events=turns, cross_track_candidates=[]), [], [], {},
            )
        assert turns.closed
        assert relation.counts == [2, 0, 0]
        assert relation._request_order == 2
        assert relation._connection.execute(
            "SELECT count(*) FROM source_requests"
        ).fetchone() == (2,)
        assert relation._connection.execute(
            "SELECT count(*) FROM annotation_requests"
        ).fetchone() == (2,)
        assert relation._connection.execute("""
          SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'writer_batch_%'
        """).fetchone() == (0,)
        relation._connection.execute("DELETE FROM turn_raw WHERE ordinal=2")
        relation.collect_sources(
            NS(turn_events=[_turn(2, ref)], cross_track_candidates=[]), [], [], {},
        )
        assert relation.counts == [3, 0, 0]
        assert [row[0] for row in relation.iter_raw("turn")] == [0, 1, 2]


def test_collect_sources_third_insert_failure_rolls_back_whole_batch(tmp_path):
    ref = RefinedPointRef("kept", 1, 1, 0)
    with _relation(tmp_path) as relation:
        relation._connection.execute(
            "INSERT INTO annotation_requests VALUES (1,'existing',0,0)"
        )
        with pytest.raises(Exception, match="duplicate key|constraint"):
            relation.collect_sources(
                NS(turn_events=[_turn(0, ref)], cross_track_candidates=[]), [], [], {},
            )
        assert relation.counts == [0, 0, 0] and relation._request_order == 0
        assert relation._connection.execute(
            "SELECT count(*) FROM source_requests"
        ).fetchone() == (0,)
        assert relation._connection.execute(
            "SELECT count(*) FROM turn_raw"
        ).fetchone() == (0,)


def test_pending_strict_length_mismatch_closes_both_iterators(tmp_path):
    ref = RefinedPointRef("kept", 1, 1, 0)
    pending = ClosingIterable([(NS(cluster_calculate_index=1), _crossing(ref))])
    crossing_ids = ClosingIterable([])
    with _relation(tmp_path) as relation:
        with pytest.raises(ValueError, match="shorter"):
            relation.collect_sources(
                NS(turn_events=[], cross_track_candidates=[]),
                pending, crossing_ids, {1: 2},
            )
        assert relation.counts == [0, 0, 0] and relation._request_order == 0
    assert pending.closed and crossing_ids.closed


@pytest.mark.parametrize("consumer", ["turn", "cross", "crossing"])
def test_missing_stable_reference_closes_active_iterators(tmp_path, consumer):
    ref = RefinedPointRef("kept", 1, 1, 0)
    turns = ClosingIterable([_turn(0, None)] if consumer == "turn" else [])
    crosses = ClosingIterable([_cross(0, ref, None)] if consumer == "cross" else [])
    pending = ClosingIterable(
        [(NS(cluster_calculate_index=1), _crossing(None))] if consumer == "crossing" else [])
    with _relation(tmp_path) as relation:
        with pytest.raises(ValueError, match="stable"):
            relation.collect_sources(NS(turn_events=turns, cross_track_candidates=crosses),
                                     pending, [4] if consumer == "crossing" else [], {1: 2})
    assert turns.closed
    if consumer != "turn":
        assert crosses.closed
    if consumer == "crossing":
        assert pending.closed


def test_empty_unresolved_and_closed_access(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        list(relation.iter_resolved_sources(0))
    relation.resolve_sources()
    assert relation.counts == [0, 0, 0]
    assert list(relation.iter_resolved_sources(0)) == []
    assert list(relation.iter_raw("turn")) == []
    assert list(relation.iter_annotation_requests()) == []
    leaf = relation._temp_directory
    relation.close()
    relation.close()
    assert not leaf.exists() and leaf.parent.is_dir()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_raw("turn"))
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_resolved_sources(0))


def test_annotations_preserve_namespaces_duplicates_hotspots_and_order(tmp_path):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    with _relation(tmp_path, batch_size=1) as relation:
        _resolve_for_annotations(relation, refs, [7, 8, 9], [7, 10])
        clusters = [
            NS(cluster_calculate_index=1, events=[
                _member("turn", 7), _member("turn", 7),
                _member("other", 8), _member("orthogonal_cross_track", 10, None),
            ]),
            NS(cluster_calculate_index=2, events=[
                _member("turn", 9), _member("orthogonal_cross_track", 999, 7),
            ]),
        ]
        hotspots = [NS(event_calculate_index=7, g_star_z=1.5),
                    NS(event_calculate_index=7, g_star_z=2.5),
                    NS(event_calculate_index=99, g_star_z=9.0)]
        relation.prepare_annotations(clusters, {1: 41, 2: 42}, hotspots)
        assert list(relation.iter_annotations("turn")) == [
            (0, 41, 2.5), (1, None, None), (2, 42, None)]
        assert list(relation.iter_annotations("cross")) == [
            (0, 42, None), (1, None, None)]
        with pytest.raises(RuntimeError, match="already prepared"):
            relation.prepare_annotations([], {}, [])


def test_annotation_last_hotspot_preserves_null_and_nan(tmp_path):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    with _relation(tmp_path, batch_size=2) as relation:
        _resolve_for_annotations(relation, refs, [7, 8], [])
        relation.prepare_annotations([], {}, [
            NS(event_calculate_index=7, g_star_z=1.0),
            NS(event_calculate_index=8, g_star_z=2.0),
            NS(event_calculate_index=7, g_star_z=None),
            NS(event_calculate_index=8, g_star_z=float("nan")),
        ])
        rows = list(relation.iter_annotations("turn"))
        assert rows[0] == (0, None, None)
        assert rows[1][:2] == (1, None) and math.isnan(rows[1][2])


def test_annotation_second_batch_sql_failure_rolls_back_all_and_retries(tmp_path):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    clusters = [NS(cluster_calculate_index=1, events=[
        _member("turn", 1), _member("turn", 2), _member("turn", 3),
    ])]
    with _relation(tmp_path, batch_size=2) as relation:
        _resolve_for_annotations(relation, refs, [1, 2, 3], [])
        relation._connection.execute(
            "CREATE UNIQUE INDEX members_encounter_unique ON members(encounter)"
        )
        relation._connection.execute("INSERT INTO members VALUES ('turn',999,2,999)")
        with pytest.raises(Exception, match="duplicate key|constraint"):
            relation.prepare_annotations(clusters, {1: 10}, [])
        assert relation._connection.execute(
            "SELECT kind,calculation_index,encounter,candidate_id FROM members"
        ).fetchall() == [("turn", 999, 2, 999)]
        assert relation._connection.execute("SELECT count(*) FROM hotspots").fetchone() == (0,)
        assert relation._connection.execute("SELECT count(*) FROM annotations").fetchone() == (0,)
        assert relation._connection.execute("""
          SELECT count(*) FROM duckdb_views() WHERE view_name LIKE 'writer_batch_%'
        """).fetchone() == (0,)
        relation._connection.execute("DELETE FROM members")
        relation.prepare_annotations(clusters, {1: 10}, [])
        assert list(relation.iter_annotations("turn")) == [
            (0, 10, None), (1, 10, None), (2, 10, None),
        ]


def test_annotation_conflict_reports_earliest_encounter_not_smallest_key(tmp_path):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation, refs, [99, 1], [])
        clusters = [
            NS(cluster_calculate_index=1, events=[_member("turn", 99), _member("turn", 1)]),
            NS(cluster_calculate_index=2, events=[_member("turn", 99), _member("turn", 1)]),
        ]
        with pytest.raises(ValueError, match=r"Detect entity 99 belongs"):
            relation.prepare_annotations(clusters, {1: 10, 2: 20}, [])
        assert relation._connection.execute("SELECT count(*) FROM members").fetchone() == (0,)
        assert relation._connection.execute("SELECT count(*) FROM annotations").fetchone() == (0,)
        relation.prepare_annotations(
            [NS(cluster_calculate_index=1, events=[_member("turn", 99)])], {1: 10}, [])
        assert list(relation.iter_annotations("turn")) == [
            (0, 10, None), (1, None, None)]


def test_annotation_state_is_fail_closed(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        relation.prepare_annotations([], {}, [])
    with pytest.raises(RuntimeError, match="not prepared"):
        list(relation.iter_annotations("turn"))
    relation.resolve_sources()
    assert list(relation.iter_annotation_requests()) == []
    relation.prepare_annotations([], {}, [])
    assert list(relation.iter_annotations("turn")) == []
    with pytest.raises(ValueError, match="unknown"):
        list(relation.iter_annotations("crossing"))
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        list(relation.iter_annotations("turn"))


@pytest.mark.parametrize("failure", [None, RuntimeError("events"), KeyboardInterrupt()])
def test_annotation_member_identity_iterator_always_closes(tmp_path, failure):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    events = ClosingIterable([_member("turn", 1)], failure)
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation, refs, [1], [])
        clusters = [NS(cluster_calculate_index=1, events=events)]
        if failure is None:
            relation.prepare_annotations(clusters, {1: 10}, [])
        else:
            with pytest.raises(type(failure)):
                relation.prepare_annotations(clusters, {1: 10}, [])
            assert relation._connection.execute("SELECT count(*) FROM members").fetchone() == (0,)
        assert events.closed


@pytest.mark.parametrize("failure", [None, RuntimeError("hotspots"), KeyboardInterrupt()])
def test_annotation_hotspot_iterator_always_closes(tmp_path, failure):
    refs = [RefinedPointRef("kept", 1, 0, 0), RefinedPointRef("kept", 2, 0, 0)]
    hotspots = ClosingIterable([NS(event_calculate_index=1, g_star_z=3.0)], failure)
    with _relation(tmp_path) as relation:
        _resolve_for_annotations(relation, refs, [1], [])
        if failure is None:
            relation.prepare_annotations([], {}, hotspots)
        else:
            with pytest.raises(type(failure)):
                relation.prepare_annotations([], {}, hotspots)
            assert relation._connection.execute("SELECT count(*) FROM hotspots").fetchone() == (0,)
        assert hotspots.closed


@pytest.mark.parametrize("batch_size", [True, 0])
def test_invalid_batch_size(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        WriterRelation(runtime_config(), tmp_path, "detect", batch_size=batch_size)


@pytest.mark.parametrize("root", ["relative", "/"])
def test_unsafe_data_root(root):
    with pytest.raises(ValueError, match="absolute and non-root"):
        WriterRelation(runtime_config(), root, "detect")


@pytest.mark.parametrize("field,value", [
    ("detect_id", "bad/name"), ("detect_id", ".."),
    ("relation_id", "bad/name"), ("relation_id", "bad\x00name"),
])
def test_unsafe_identity(tmp_path, field, value):
    kwargs = {field: value}
    detect_id = kwargs.pop("detect_id", "detect")
    with pytest.raises(ValueError, match=field):
        WriterRelation(runtime_config(), tmp_path, detect_id, **kwargs)


def test_connection_and_schema_failures_clean_only_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "writer-relations" / "detect" / "keep"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(writer_relation.duckdb, "connect",
                        lambda **_kw: (_ for _ in ()).throw(RuntimeError("open failed")))
    with pytest.raises(RuntimeError, match="open failed"):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="failed")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_schema_failure_closes_connection_and_cleans_only_leaf(tmp_path, monkeypatch):
    sibling = tmp_path / "duckdb" / "writer-relations" / "detect" / "keep"
    sibling.mkdir(parents=True)
    real_connect = writer_relation.duckdb.connect
    connection = real_connect(":memory:")
    monkeypatch.setattr(writer_relation.duckdb, "connect", lambda **_kw: connection)
    monkeypatch.setattr(WriterRelation, "_create_schema",
                        lambda _self: (_ for _ in ()).throw(RuntimeError("schema failed")))
    with pytest.raises(RuntimeError, match="schema failed"):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="failed")
    with pytest.raises(Exception):
        connection.execute("SELECT 1")
    assert sibling.is_dir() and not (sibling.parent / "failed").exists()


def test_identity_collision_preserves_foreign_leaf(tmp_path):
    leaf = tmp_path / "duckdb" / "writer-relations" / "detect" / "occupied"
    leaf.mkdir(parents=True)
    marker = leaf / "owned"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        WriterRelation(runtime_config(), tmp_path, "detect", relation_id="occupied")
    assert marker.read_text() == "keep"


def test_offline_limits_and_columnar_primitives(tmp_path):
    with _relation(tmp_path) as relation:
        names = ("threads", "memory_limit", "max_temp_directory_size", "temp_directory",
                 "allow_unsigned_extensions",
                 "autoload_known_extensions", "autoinstall_known_extensions")
        settings = dict(relation._connection.execute(
            "SELECT name,value FROM duckdb_settings() WHERE name IN (?,?,?,?,?,?,?)", names
        ).fetchmany(20))
        assert settings == {
            "threads": "1", "memory_limit": "122.0 MiB",
            "max_temp_directory_size": "244.1 MiB",
            "temp_directory": str(relation._temp_directory),
            "allow_unsigned_extensions": "false",
            "autoload_known_extensions": "false", "autoinstall_known_extensions": "false",
        }
    source = inspect.getsource(writer_relation).lower()
    assert 'database=":memory:"' in source and "fetchmany" in source and "order by" in source
    for forbidden in ("fetchall", "pickle", "heapq", "tempfile"):
        assert forbidden not in source


def _prepared_final_relation(tmp_path, *, batch_size=1, cross_refs=None):
    refs = [RefinedPointRef("kept", index, 0, 0) for index in range(1, 7)]
    relation = _relation(tmp_path, batch_size=batch_size)
    crosses = cross_refs or [(refs[1], refs[0]), (refs[2], refs[3]), (refs[5], refs[4])]
    relation.collect_sources(
        NS(turn_events=[_turn(8, refs[0]), _turn(9, refs[1]), _turn(10, refs[2])],
           cross_track_candidates=[_cross(index + 20, *pair)
                                   for index, pair in enumerate(crosses)]),
        [(NS(cluster_calculate_index=7), _crossing(refs[3])),
         (NS(cluster_calculate_index=8),
          NS(segment_start=refs[4], is_entry=False, crossing_x=3.0,
             crossing_y=4.0, crossing_lon=114.0, crossing_lat=24.0))],
        [900, 901], {7: 70, 8: 80},
    )
    for index, ref in enumerate(refs, 101):
        relation.add_mapping(ref, index)
    relation.resolve_sources()
    relation.prepare_annotations([
        NS(cluster_calculate_index=7, events=[
            _member("turn", 8), _member("orthogonal_cross_track", 0, 20),
        ]),
        NS(cluster_calculate_index=8, events=[
            _member("turn", 10), _member("orthogonal_cross_track", 0, 22),
        ]),
    ], {7: 70, 8: 80}, [NS(event_calculate_index=8, g_star_z=4.5)])
    return relation


def test_final_rows_exact_copy_shape_and_original_order_across_batches(tmp_path):
    with _prepared_final_relation(tmp_path) as relation:
        assert list(relation.iter_rows(0, "detect")) == [
            ("detect", 70, 101, 18.0, 20.0, -10.0, 0.5, 4.5),
            ("detect", None, 102, 19.0, 20.0, -10.0, 0.5, None),
            ("detect", 80, 103, 20.0, 20.0, -10.0, 0.5, None),
        ]
        expected_cross = [
            ("detect", 70, 101, 102, 20.1, 2.0, 113.0, 23.0),
            ("detect", None, 103, 104, 21.1, 2.0, 113.0, 23.0),
            ("detect", 80, 105, 106, 22.1, 2.0, 113.0, 23.0),
        ]
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_rows(1, "detect"))
        assert list(relation.iter_rows(2, "detect")) == [
            (900, "detect", 70, 104, 1, 1.0, 2.0, 113.0, 23.0),
            (901, "detect", 80, 105, 0, 3.0, 4.0, 114.0, 24.0),
        ]
        relation.validate_cross_pairs("detect")
        assert list(relation.iter_validated_cross_rows()) == expected_cross
        assert list(relation.iter_rows(1, "detect")) == expected_cross
        relation.validate_cross_pairs("detect")


@pytest.mark.parametrize("pairs", [
    ((0, 1), (1, 0), (2, 3)),
    ((0, 1), (0, 1), (2, 3)),
    ((0, 0), (1, 2), (3, 4)),
])
def test_cross_pair_validation_is_atomic_for_duplicates_and_degenerate(tmp_path, pairs):
    refs = [RefinedPointRef("kept", index, 0, 0) for index in range(1, 7)]
    relation = _prepared_final_relation(
        tmp_path, cross_refs=[(refs[first], refs[second]) for first, second in pairs],
    )
    try:
        with pytest.raises(ValueError, match="duplicate/degenerate track cross segment pair"):
            relation.validate_cross_pairs("detect")
        assert relation._connection.execute(
            "SELECT count(*) FROM validated_cross"
        ).fetchone() == (0,)
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_validated_cross_rows())
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_rows(1, "detect"))
    finally:
        relation.close()


@pytest.mark.parametrize("mutation,error", [
    ("DELETE FROM resolved_sources WHERE consumer=0 AND ordinal=0", "source"),
    ("INSERT INTO resolved_sources VALUES (0,99,0,1)", "source"),
    ("INSERT INTO resolved_sources VALUES (0,0,0,1)", "source"),
    ("DELETE FROM annotations WHERE kind='turn' AND ordinal=0", "annotation"),
    ("INSERT INTO annotations VALUES (999,'turn',99,NULL,NULL)", "annotation"),
    ("DELETE FROM turn_raw WHERE ordinal=0", "raw"),
])
def test_final_rows_fail_closed_for_tampered_relations(tmp_path, mutation, error):
    with _prepared_final_relation(tmp_path) as relation:
        relation._connection.execute(mutation)
        rows = relation.iter_rows(0, "detect")
        with pytest.raises(RuntimeError, match=error):
            next(rows)


def test_cross_validation_association_failure_never_exposes_prefix(tmp_path):
    with _prepared_final_relation(tmp_path) as relation:
        relation._connection.execute(
            "DELETE FROM annotations WHERE kind='cross' AND ordinal=2"
        )
        with pytest.raises(RuntimeError, match="annotation"):
            relation.validate_cross_pairs("detect")
        assert relation._connection.execute(
            "SELECT count(*) FROM validated_cross"
        ).fetchone() == (0,)
        with pytest.raises(RuntimeError, match="not validated"):
            next(relation.iter_validated_cross_rows())


def test_final_row_states_identity_empty_and_close(tmp_path):
    relation = _relation(tmp_path)
    with pytest.raises(RuntimeError, match="not resolved"):
        next(relation.iter_rows(2, "detect"))
    relation.resolve_sources()
    assert list(relation.iter_rows(2, "detect")) == []
    with pytest.raises(RuntimeError, match="not prepared"):
        next(relation.iter_rows(0, "detect"))
    relation.prepare_annotations([], {}, [])
    for consumer in (0, 2):
        assert list(relation.iter_rows(consumer, "detect")) == []
    with pytest.raises(RuntimeError, match="not validated"):
        next(relation.iter_rows(1, "detect"))
    relation.validate_cross_pairs("detect")
    assert list(relation.iter_validated_cross_rows()) == []
    assert list(relation.iter_rows(1, "detect")) == []
    with pytest.raises(ValueError, match="unknown"):
        next(relation.iter_rows(3, "detect"))
    with pytest.raises(ValueError, match="does not match"):
        next(relation.iter_rows(0, "other"))
    with pytest.raises(ValueError, match="does not match"):
        relation.validate_cross_pairs("other")
    relation.close()
    with pytest.raises(RuntimeError, match="closed"):
        next(relation.iter_rows(0, "detect"))
    with pytest.raises(RuntimeError, match="closed"):
        relation.validate_cross_pairs("detect")
    with pytest.raises(RuntimeError, match="closed"):
        next(relation.iter_validated_cross_rows())
