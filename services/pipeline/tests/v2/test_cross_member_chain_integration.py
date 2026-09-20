"""Isolated prototype: exact eager/lazy member chain and output lifetime parity."""
from array import array
from contextlib import ExitStack
from dataclasses import replace
import gc
import json
import pickle
from pathlib import Path
from unittest.mock import patch
import weakref

import pytest

from track.v2 import detect
from track.v2.candidate_extraction import extract_cross_track_candidates, extract_disk_cross_track_candidates
from track.v2.cross_member_store import CandidateStore, ClusterMemberSequence
from track.v2.member_sequences import ConcatSequence
from track.v2.db_writer import _insert_tracks_cross, _refine_key
from track.v2.writer_workspace import WriterWorkspace
from track.v2.models import RefinedPointRef, RuntimeConfig
from track.v2.detect_workspace import DetectWorkspace
from track.v2.models import HotspotCluster, PointRef
from .test_ablation_centroid_memory import _crossing, _event
from .test_detect import _TEST_TRANSFORMER
from .test_detect_disk_workspace import CONTEXT, TRANSFORMER, crossing_tracks



def membership_rows(clusters, ids):
    with WriterWorkspace(batch_size=3, merge_fan_in=2) as ws:
        ws.prepare_annotations(clusters, ids, [])
        return list(ws.records("unique_members"))


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
    spool = CandidateStore(directory=tmp_path)
    for candidate in values:
        spool.append(candidate)
    spool.seal()
    return spool


def serialized(clusters):
    # String equality also checks emitted representation, key and member order.
    return json.dumps([cluster.as_dict() for cluster in clusters], allow_nan=True)


def cluster_pass_ids(spool):
    return [row[0] for row in spool._db.execute("SELECT DISTINCT pass_id FROM members ORDER BY pass_id")]


@pytest.mark.parametrize("merge_distance", [0., 30., 100.])
def test_complete_pipeline_matches_eager_order_refs_ablation_and_writer_membership(tmp_path, merge_distance):
    values = candidates()
    turns = [replace(_event(7 + i * 101, values[i].x, values[i].y),
                     point_ref=PointRef("turn-source", str(i % 2), True),
                     source_ids=("turn-event-source",)) for i in range(4)]
    cfg = detect.IntersectConfig(
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
        assert membership_rows(actual, ids) == membership_rows(expected, ids) == eager_membership_rows(expected, ids)
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
    cfg = detect.IntersectConfig(cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2,
                                dbscan_eps_m_turn=2., dbscan_min_samples_turn=2,
                                merge_centroid_distance_m=100.)
    real_loads = pickle.loads
    real_event = ClusterMemberSequence._event
    decoded = []
    wrapped = []

    def observe_loads(payload, **kwargs):
        candidate = real_loads(payload, **kwargs)
        decoded.extend((weakref.ref(candidate), weakref.ref(candidate.point_ref)))
        return candidate

    def observe_event(candidate, index):
        event = real_event(candidate, index)
        wrapped.append(weakref.ref(event))
        return event

    with stored(tmp_path, values) as spool:
        del values
        with patch("track.v2.cross_member_store.pickle.loads", side_effect=observe_loads), patch.object(
            ClusterMemberSequence, "_event", new=staticmethod(observe_event),
        ):
            clusters = detect._build_candidate_clusters([], spool, cfg, _TEST_TRANSFORMER)
            gc.collect()
            assert decoded
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
    cfg = detect.IntersectConfig(cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2)
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


def test_sorted_extraction_to_result_survives_track_workspace_and_writes_same_rows(tmp_path):
    # The geometry-only fixture reuses (kept, 10000, subtrack=1) across
    # vehicles. Persistence needs globally distinct Refine source identities.
    # Keep every coordinate, source PointRef and injected/kept kind unchanged.
    tracks = {
        f"{points[0].vehicle_id}\x00{track_index}": [
            replace(point, refined_point_ref=replace(
                point.refined_point_ref, point_id=track_index * 10000 + point_index,
                subtrack_index=track_index,
            ))
            for point_index, point in enumerate(points)
        ]
        for track_index, points in enumerate(crossing_tracks().values(), 1)
    }
    source_keys = [_refine_key(point.refined_point_ref) for points in tracks.values() for point in points]
    assert len(source_keys) == len(set(source_keys))
    cfg = detect.IntersectConfig(cross_track_pair_batch_size=3, use_turn_events=False,
                                cross_track_dbscan_min_samples=1)
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    spool = CandidateStore(directory=tmp_path)
    try:
        with DetectWorkspace(cfg, directory=tmp_path) as workspace:
            for key in reversed(list(tracks)):
                workspace[key] = tracks[key]
            actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg,
                                                         candidate_store=spool)
        assert actual is spool
        assert actual == expected
        _, eager_clusters = detect._cluster_pipeline([], expected, TRANSFORMER, cfg)
        _, lazy_clusters = detect._cluster_pipeline([], spool, TRANSFORMER, cfg)
        result = replace(detect._empty_intersect_result(CONTEXT), clusters=lazy_clusters,
                         cross_track_candidates=spool)
        eager = replace(result, clusters=eager_clusters, cross_track_candidates=expected)
        assert result.as_dict() == eager.as_dict()
        ids = {cluster.cluster_calculate_index: 100 + i for i, cluster in enumerate(lazy_clusters)}
        assert membership_rows(lazy_clusters, ids) == eager_membership_rows(eager_clusters, ids)
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
            with WriterWorkspace(directory=tmp_path, batch_size=3) as writer_workspace:
                writer_workspace.collect_sources(output, [], (), ids)
                for (kind, point_id, subtrack), rid in resolved.items():
                    writer_workspace.add_mapping(RefinedPointRef(kind, point_id, subtrack, 0), rid)
                writer_workspace.resolve_sources()
                writer_workspace.prepare_annotations(output.clusters, ids, output.hotspot_events)
                with patch("track.v2.db_writer._copy_insert", side_effect=capture):
                    _insert_tracks_cross(Connection(), "run", writer_workspace, batch_size=3)
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
        assert not list(tmp_path.iterdir())
        with pytest.raises(RuntimeError):
            list(lazy_clusters[0].events)
    finally:
        spool.close()


@pytest.mark.parametrize("phase", ["reader", "clustering", "topology"])
@pytest.mark.parametrize("failure", [RuntimeError("stage failed"), KeyboardInterrupt()])
def test_db_owner_closes_all_spools_on_failure(tmp_path, monkeypatch, phase, failure):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tracks = crossing_tracks()
    cfg = detect.IntersectConfig(use_turn_events=False, cross_track_dbscan_min_samples=1)
    with ExitStack() as stack:
        stack.enter_context(patch.object(detect, "_query_spatial_extent", return_value=(0., 0., 1., 1.)))
        stack.enter_context(patch.object(detect, "_stream_vehicle_tracks_from_db", return_value=iter(tracks.values())))
        target = {"reader": "_load_tracks_and_turn_candidates", "clustering": "_cluster_pipeline",
                  "topology": "_build_topologies_from_tracks"}[phase]
        stack.enter_context(patch.object(detect, target, side_effect=failure))
        with pytest.raises(type(failure)):
            detect._detect_intersections_db(
                object(), cfg, CONTEXT, runtime=RuntimeConfig(), data_root=tmp_path,
            )
    assert not list(tmp_path.glob("duckdb/turn-event-relations/*"))


def test_db_owner_returns_live_candidate_store_and_complete_eager_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tracks = crossing_tracks()
    cfg = detect.IntersectConfig(use_turn_events=False, cross_track_dbscan_min_samples=1)
    expected_candidates, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    hotspots, clusters = detect._cluster_pipeline([], expected_candidates, TRANSFORMER, cfg)
    topologies, clusters, observations = detect._build_topologies_from_tracks(clusters, tracks, cfg, TRANSFORMER)
    eager = detect.IntersectResult(
        turn_events=[], hotspot_events=hotspots, clusters=clusters, topologies=topologies,
        observations=observations, cross_track_candidates=expected_candidates, spatial_context=CONTEXT,
        report=detect._build_intersect_report(clusters, topologies, [], hotspots, expected_candidates, len(tracks)),
    )
    with patch.object(detect, "_query_spatial_extent", return_value=(0., 0., 1., 1.)), patch.object(
        detect, "_stream_vehicle_tracks_from_db", return_value=iter(tracks.values()),
    ):
        actual = detect._detect_intersections_db(
            object(), cfg, CONTEXT, runtime=RuntimeConfig(), data_root=tmp_path,
        )
    try:
        assert isinstance(actual.cross_track_candidates, CandidateStore)
        assert actual.as_dict() == eager.as_dict()
        assert actual.as_dict() == eager.as_dict()
    finally:
        actual.close()
    assert not list(tmp_path.glob("duckdb/turn-event-relations/*"))


def test_summary_merge_and_reference_lengths_do_not_decode_members_again(tmp_path):
    values = candidates()
    cfg = detect.IntersectConfig(
        dbscan_eps_m_turn=2., dbscan_min_samples_turn=2,
        cross_track_dbscan_eps_m=2., cross_track_dbscan_min_samples=2,
        merge_centroid_distance_m=100.,
    )
    turns = [_event(i, values[i].x, values[i].y) for i in range(4)]
    real_loads = pickle.loads
    decoded = 0

    def count_loads(payload, **kwargs):
        nonlocal decoded
        decoded += 1
        return real_loads(payload, **kwargs)

    with stored(tmp_path, values) as spool:
        with patch("track.v2.cross_member_store.pickle.loads", new=count_loads):
            crosses = detect._cluster_orthogonal_cross_track_candidates(
                spool, _TEST_TRANSFORMER, eps_m=2., min_samples=2,
            )
        turn_clusters = detect._cluster_dbscan(
            turns, _TEST_TRANSFORMER, eps_m=2., min_samples=2, source="turn",
        )
        # Each pass builds summaries during at most one full global-order scan.
        assert 0 < decoded <= len(values)
        assert len(crosses) > 1
        with patch("track.v2.cross_member_store.pickle.loads", side_effect=AssertionError("decoded cached provenance")):
            initial = detect._merge_clusters([*turn_clusters, *crosses], 100., _TEST_TRANSFORMER)
            normalized = [detect._with_source_provenance(cluster) for cluster in initial]
            assert sum(len(refs) for cluster in normalized for refs in cluster.source_point_refs.values()) == 16
        before_ablation = decoded
        with patch("track.v2.cross_member_store.pickle.loads", new=count_loads):
            annotated = detect._annotate_source_ablation(initial, turns, spool, cfg, _TEST_TRANSFORMER)
        assert 0 < decoded - before_ablation <= len(values)
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
                membership_rows([first, duplicate], ids)
        with pytest.raises(ValueError, match="belongs to multiple candidates"):
            eager_membership_rows([eager[0], replace(eager[0], cluster_calculate_index=999)],
                                  {eager[0].cluster_calculate_index: 10, 999: 20})
