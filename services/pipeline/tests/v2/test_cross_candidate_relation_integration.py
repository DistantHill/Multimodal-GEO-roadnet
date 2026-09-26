from .config_factory import (clip_config, fitting_config, intersect_config,
                             intersect_run_config, network_config, postgres_config,
                             projection_config, refine_config, runtime_config)
"""Isolated prototype: exact eager/lazy member chain and output lifetime parity."""
from array import array
from contextlib import ExitStack
from dataclasses import replace
import gc
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import weakref

import pytest

import track.v2.intersections.detect_intersections as detect
from track.v2.candidates.cross_track_relation import ClusterMemberSequence, CrossCandidateRelation
from track.v2.intersections.member_sequences import ConcatSequence
from track.v2.db.write_detect_results import _insert_tracks_cross, _refine_key
from track.v2.intersections.relations.source_associations import WriterRelation
from track.v2.models import RefinedPointRef, RuntimeConfig
from track.v2.models import HotspotCluster, PointRef
from .test_ablation_centroid_memory import _crossing, _event
from .test_detect import _TEST_SPATIAL_CONTEXT, _TEST_TRANSFORMER



def membership_rows(tmp_path, clusters, ids):
    with WriterRelation(runtime_config(), tmp_path, "membership", batch_size=3) as relation:
        relation.resolve_sources()
        relation.prepare_annotations(clusters, ids, [])
        return relation._connection.execute("""
            SELECT kind,calculation_index,candidate_id FROM members
            QUALIFY row_number() OVER (
                PARTITION BY kind,calculation_index ORDER BY encounter
            )=1 ORDER BY kind,calculation_index
        """).fetchall()


def eager_membership_rows(clusters, ids):
    # Independent historical dictionary oracle, confined to small test data.
    mappings = {"turn": {}, "cross": {}}
    for cluster in clusters:
        candidate_id = ids[cluster.cluster_calculate_index]
        for event in cluster.events:
            if event.source == "turn":
                kind, index = "turn", event.event_calculate_index
            elif event.source == "orthogonal_cross_track" and event.cross_track_calculate_index is not None:
                kind, index = "cross", event.cross_track_calculate_index
            else:
                continue
            previous = mappings[kind].get(index)
            if previous is not None and previous != candidate_id:
                raise ValueError(f"Detect entity {index} belongs to multiple candidates")
            mappings[kind][index] = candidate_id
    return sorted((kind, index, value) for kind, mapping in mappings.items() for index, value in mapping.items())


def candidates(count=17):
    x, y = _TEST_TRANSFORMER.forward(116., 39.)
    # Two equally sized groups, exact eps contacts, duplicates and one noise.
    return [replace(
        _crossing(i, x + (i // 8) * 30 + (i % 4) * 2, y),
        cross_track_calculate_index=i,
        point_ref=PointRef("repeated", str(i % 3), True),
    ) for i in range(count - 1)] + [replace(
        _crossing(count - 1, x + 10000., y), cross_track_calculate_index=count - 1,
    )]


def stored(tmp_path, values):
    spool = CrossCandidateRelation(runtime_config(), tmp_path)
    for candidate in values:
        spool.append(candidate)
    spool.seal()
    return spool


def serialized(clusters):
    # String equality also checks emitted representation, key and member order.
    return json.dumps([cluster.as_dict() for cluster in clusters], allow_nan=True)


def cluster_pass_ids(spool):
    return [row[0] for row in spool._connection.execute(
        "SELECT DISTINCT pass_id FROM cross_candidate_members ORDER BY pass_id"
    ).fetchall()]


@pytest.mark.parametrize("merge_distance", [0., 30., 100.])
def test_complete_pipeline_matches_eager_order_refs_ablation_and_writer_membership(tmp_path, merge_distance):
    values = candidates()
    turns = [replace(_event(7 + i * 101, values[i].x, values[i].y),
                     point_ref=PointRef("turn-source", str(i % 2), True),
                     source_ids=("turn-event-source",)) for i in range(4)]
    cfg = intersect_config(
        g_star_min_z=-100., g_star_radius_m=15.,
        dbscan_eps_m_turn=2., dbscan_min_samples_turn=2,
        cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2,
        merge_centroid_distance_m=merge_distance,
    )
    expected_hotspots, expected = detect._cluster_pipeline(turns, values, _TEST_TRANSFORMER, cfg)
    with stored(tmp_path, values) as spool:
        actual_hotspots, actual = detect._cluster_pipeline(turns, spool, _TEST_TRANSFORMER, cfg)
        assert actual_hotspots == expected_hotspots
        assert serialized(actual) == serialized(expected)
        assert serialized(actual) == serialized(expected)  # pass remains reusable
        assert len(cluster_pass_ids(spool)) == 1  # ablation passes were removed
        assert spool[-1] == values[-1]  # noise retained independently of members
        ids = {cluster.cluster_calculate_index: 100 + i for i, cluster in enumerate(actual)}
        assert (membership_rows(tmp_path, actual, ids)
                == membership_rows(tmp_path, expected, ids)
                == eager_membership_rows(expected, ids))
        for cluster, original in zip(actual, expected):
            assert cluster.events == original.events
            assert cluster.events[::-1] == original.events[::-1]
            assert cluster.source_point_refs == original.source_point_refs


@pytest.mark.parametrize("count", [17, 273])
def test_cross_dbscan_uses_same_kernel_and_compact_float_arrays(tmp_path, count):
    values = candidates(count)
    expected = detect._cluster_orthogonal_cross_track_candidates(
        values, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
    )
    real_dbscan = detect._run_dbscan

    def compact(xs, ys, **kwargs):
        assert isinstance(xs, array) and xs.typecode == "d"
        assert isinstance(ys, array) and ys.typecode == "d"
        assert all(type(value) is float for value in xs)
        assert list(xs) == [candidate.x for candidate in values]
        assert list(ys) == [candidate.y for candidate in values]
        return real_dbscan(xs, ys, **kwargs)

    with stored(tmp_path, values) as spool:
        with patch.object(detect, "_run_dbscan", side_effect=compact), patch.object(
            detect, "_group_cluster_items", side_effect=AssertionError("full object grouping"),
        ):
            actual = detect._cluster_orthogonal_cross_track_candidates(
                spool, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
            )
        assert serialized(actual) == serialized(expected)
        assert all(isinstance(cluster.events, ClusterMemberSequence) for cluster in actual)


def test_lazy_members_merge_and_provenance_do_not_retain_decoded_payloads(tmp_path):
    values = candidates(129)
    cfg = intersect_config(cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2,
                                dbscan_eps_m_turn=2., dbscan_min_samples_turn=2,
                                merge_centroid_distance_m=100.)
    real_decode = CrossCandidateRelation._decode.__func__
    real_event = ClusterMemberSequence._event
    decoded = []
    wrapped = []

    def observe_decode(cls, row):
        candidate = real_decode(cls, row)
        decoded.extend((weakref.ref(candidate), weakref.ref(candidate.point_ref)))
        return candidate

    def observe_event(candidate, index):
        event = real_event(candidate, index)
        wrapped.append(weakref.ref(event))
        return event

    with stored(tmp_path, values) as spool:
        del values
        with patch.object(CrossCandidateRelation, "_decode", new=classmethod(observe_decode)), patch.object(
            ClusterMemberSequence, "_event", new=staticmethod(observe_event),
        ):
            clusters = detect._build_candidate_clusters([], spool, cfg, _TEST_TRANSFORMER)
            gc.collect()
            assert not decoded
            assert not wrapped  # summary construction never needs TurnEvent payloads
            assert all(reference() is None for reference in decoded + wrapped)
            assert all(isinstance(cluster.events, ConcatSequence) for cluster in clusters)
            # Supply turns so ablation also constructs and releases cross passes.
            first = clusters[0]
            turns = [_event(i, first.centroid_x, first.centroid_y) for i in range(4)]
            combined = detect._build_candidate_clusters(turns, spool, cfg, _TEST_TRANSFORMER)
            previous = cluster_pass_ids(spool)
            annotated = detect._annotate_source_ablation(combined, turns, spool, cfg, _TEST_TRANSFORMER)
            assert cluster_pass_ids(spool) == previous
            gc.collect()
            assert all(reference() is None for reference in decoded + wrapped)
            assert serialized(annotated)
            assert decoded
            assert wrapped  # explicit export is the point where events are decoded
            gc.collect()
            assert all(reference() is None for reference in decoded + wrapped)


def test_manual_reference_source_ids_and_duplicate_order_survive_lazy_merge(tmp_path):
    values = candidates()
    with stored(tmp_path, values) as spool:
        lazy = detect._cluster_orthogonal_cross_track_candidates(
            spool, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
        )
        eager = detect._cluster_orthogonal_cross_track_candidates(
            values, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
        )
        event = _event(31, lazy[0].centroid_x, lazy[0].centroid_y)
        extra = PointRef("manual-only", "outside-events", True)
        manual = HotspotCluster(91, event.x, event.y, event.lon, event.lat, 2.,
                                events=(event,), source_point_refs={"turn": (extra, extra)})
        actual = detect._merge_clusters([manual, *lazy], 100., _TEST_TRANSFORMER)
        expected = detect._merge_clusters([manual, *eager], 100., _TEST_TRANSFORMER)
        assert serialized(actual) == serialized(expected)
        assert "manual-only" in actual[0].source_ids
        assert list(actual[0].source_point_refs["turn"]) == [extra, extra]


@pytest.mark.parametrize("failure", [RuntimeError("ablation failed"), KeyboardInterrupt()])
def test_ablation_failure_closes_only_its_pass(tmp_path, failure):
    values = candidates()
    cfg = intersect_config(cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2)
    with stored(tmp_path, values) as spool:
        main = detect._build_candidate_clusters([], spool, cfg, _TEST_TRANSFORMER)
        before = serialized(main)
        passes = cluster_pass_ids(spool)
        # Add the turn source, forcing the first ablation to build cross members.
        mixed = [replace(main[0], algorithm_sources=("turn", "orthogonal_cross_track"))]
        real_build = detect._build_candidate_clusters

        def fail(*args, **kwargs):
            kwargs["disabled_sources"] = {"turn"}
            real_build(*args, **kwargs)
            raise failure

        with patch.object(detect, "_build_candidate_clusters", side_effect=fail):
            with pytest.raises(type(failure)):
                detect._annotate_source_ablation(mixed, [], spool, cfg, _TEST_TRANSFORMER)
        assert cluster_pass_ids(spool) == passes
        assert serialized(main) == before


def test_sorted_relation_to_result_writes_same_rows(tmp_path):
    cfg = intersect_config(cross_track_pair_batch_size=3, use_turn_events=False,
                                 cross_track_dbscan_min_samples=1)
    expected = [replace(
        value,
        from_segment_start=RefinedPointRef("kept", index * 2 + 1, index, 0),
        to_segment_start=RefinedPointRef("kept", index * 2 + 2, index, 0),
    ) for index, value in enumerate(candidates(8), 1)]
    spool = CrossCandidateRelation(runtime_config(), tmp_path)
    try:
        for value in expected:
            spool.append(value)
        spool.seal()
        actual = spool
        assert actual is spool
        assert actual == expected
        _, eager_clusters = detect._cluster_pipeline([], expected, _TEST_TRANSFORMER, cfg)
        _, lazy_clusters = detect._cluster_pipeline([], spool, _TEST_TRANSFORMER, cfg)
        result = replace(detect._empty_intersect_result(_TEST_SPATIAL_CONTEXT), clusters=lazy_clusters,
                         cross_track_candidates=spool)
        eager = replace(result, clusters=eager_clusters, cross_track_candidates=expected)
        assert result.as_dict() == eager.as_dict()
        ids = {cluster.cluster_calculate_index: 100 + i for i, cluster in enumerate(lazy_clusters)}
        assert membership_rows(tmp_path, lazy_clusters, ids) == eager_membership_rows(eager_clusters, ids)
        refined = {_refine_key(ref) for candidate in expected
                   for ref in (candidate.from_segment_start, candidate.to_segment_start)}
        resolved = {ref: i + 1 for i, ref in enumerate(sorted(refined))}
        expected_pairs = [tuple(sorted((resolved[_refine_key(candidate.from_segment_start)],
                                        resolved[_refine_key(candidate.to_segment_start)])))
                          for candidate in expected]
        assert all(a != b for a, b in expected_pairs)
        assert len(expected_pairs) == len(set(expected_pairs))

        class Connection:
            def cursor(self):
                return ExitStack()

        rows = []
        for output in (result, eager):
            copied = []
            def capture(_cursor, _table, _columns, values, **kwargs):
                assert len(values) <= 3
                copied.extend(values)
            with WriterRelation(runtime_config(), tmp_path, "run", batch_size=3) as relation:
                relation.collect_sources(output, [], (), ids)
                for (kind, point_id, subtrack), rid in resolved.items():
                    relation.add_mapping(RefinedPointRef(kind, point_id, subtrack, 0), rid)
                relation.resolve_sources()
                relation.prepare_annotations(output.clusters, ids, output.hotspot_events)
                with patch("track.v2.db.write_detect_results._copy_insert", side_effect=capture):
                    _insert_tracks_cross(Connection(), "run", relation, batch_size=3)
            rows.append(copied)
        membership = {index: value for kind, index, value in eager_membership_rows(eager_clusters, ids) if kind == "cross"}
        expected_rows = []
        for candidate in expected:
            a, b = sorted((resolved[_refine_key(candidate.from_segment_start)],
                           resolved[_refine_key(candidate.to_segment_start)]))
            expected_rows.append(("run", membership.get(candidate.cross_track_calculate_index), a, b,
                                  candidate.x, candidate.y, candidate.lon, candidate.lat))
        assert rows[0] == rows[1] == expected_rows
        assert result.as_dict() == eager.as_dict()
        result.close()
        result.close()
        assert not list(tmp_path.glob("duckdb/cross-candidate-relations/*"))
        with pytest.raises(RuntimeError):
            list(lazy_clusters[0].events)
    finally:
        spool.close()


@pytest.mark.parametrize("phase", ["reader", "clustering", "topology"])
@pytest.mark.parametrize("failure", [RuntimeError("stage failed"), KeyboardInterrupt()])
def test_db_owner_closes_all_spools_on_failure(tmp_path, monkeypatch, phase, failure):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    cfg = intersect_run_config(
        use_turn_events=False, cross_track_dbscan_min_samples=1,
        refine_id="refine-1",
    )
    reader = MagicMock()
    reader.extent.return_value = (0., 0., 1., 1.)
    reader.iter_cross_track_pair_batches.return_value = iter(())
    reader.iter_topology_tracks.return_value = iter(())
    reader.track_count.return_value = 0
    with ExitStack() as stack:
        target = {"reader": "run_grid_cross_track_task", "clustering": "_cluster_pipeline",
                  "topology": "_build_topologies_from_tracks"}[phase]
        if phase != "reader":
            stack.enter_context(patch.object(
                detect, "run_grid_cross_track_task", return_value=[],
            ))
        stack.enter_context(patch.object(detect, target, side_effect=failure))
        with pytest.raises(type(failure)):
            detect._detect_intersections_db(
                object(), cfg, _TEST_SPATIAL_CONTEXT, runtime=runtime_config(),
                data_root=tmp_path, refine_reader=reader,
                connect_worker=lambda: object(),
            )
    assert not list(tmp_path.glob("duckdb/turn-event-relations/*"))
    assert not list(tmp_path.glob("duckdb/cross-candidate-relations/*"))


def test_summary_merge_and_reference_lengths_do_not_decode_members_again(tmp_path):
    values = candidates()
    cfg = intersect_config(
        dbscan_eps_m_turn=2., dbscan_min_samples_turn=2,
        cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2,
        merge_centroid_distance_m=100.,
    )
    turns = [_event(i, values[i].x, values[i].y) for i in range(4)]
    real_decode = CrossCandidateRelation._decode.__func__
    decoded = 0

    def count_decode(cls, row):
        nonlocal decoded
        decoded += 1
        return real_decode(cls, row)

    with stored(tmp_path, values) as spool:
        with patch.object(CrossCandidateRelation, "_decode", new=classmethod(count_decode)):
            crosses = detect._cluster_orthogonal_cross_track_candidates(
                spool, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
            )
        turn_clusters = detect._cluster_dbscan(
            turns, _TEST_TRANSFORMER, eps_m=2., min_samples=2, source="turn",
        )
        # Narrow provenance summaries do not reconstruct candidate payloads.
        assert decoded == 0
        assert len(crosses) > 1
        with patch.object(CrossCandidateRelation, "_decode", side_effect=AssertionError("decoded cached provenance")):
            initial = detect._merge_clusters([*turn_clusters, *crosses], 100., _TEST_TRANSFORMER)
            normalized = [detect._with_source_provenance(cluster) for cluster in initial]
            assert sum(len(refs) for cluster in normalized for refs in cluster.source_point_refs.values()) == 16
        before_ablation = decoded
        with patch.object(CrossCandidateRelation, "_decode", new=classmethod(count_decode)):
            annotated = detect._annotate_source_ablation(initial, turns, spool, cfg, _TEST_TRANSFORMER)
        assert decoded == before_ablation
        assert serialized(annotated) == serialized(detect._annotate_source_ablation(
            detect._build_candidate_clusters(turns, values, cfg, _TEST_TRANSFORMER),
            turns, values, cfg, _TEST_TRANSFORMER,
        ))


def test_eager_and_lazy_membership_conflicts_still_raise(tmp_path):
    values = candidates()
    with stored(tmp_path, values) as spool:
        lazy = detect._cluster_orthogonal_cross_track_candidates(spool, _TEST_TRANSFORMER, eps_m=2., min_samples=2)
        eager = detect._cluster_orthogonal_cross_track_candidates(values, _TEST_TRANSFORMER, eps_m=2., min_samples=2)
        for groups in (eager, lazy):
            first = groups[0]
            duplicate = replace(first, cluster_calculate_index=999)
            ids = {first.cluster_calculate_index: 10, 999: 20}
            with pytest.raises(ValueError, match="belongs to multiple candidates"):
                membership_rows(tmp_path, [first, duplicate], ids)
        with pytest.raises(ValueError, match="belongs to multiple candidates"):
            eager_membership_rows([eager[0], replace(eager[0], cluster_calculate_index=999)],
                                  {eager[0].cluster_calculate_index: 10, 999: 20})
